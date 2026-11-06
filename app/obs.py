"""Logs: request ids, the attendee a request is for, secret redaction, and
the JSON format (LOG_FORMAT=json) for production; plain text stays the default.

Every log line made while handling a request carries its `request_id` (from
X-Request-ID or Cloudflare's CF-Ray when sent, else a new one, and echoed in
the response's X-Request-ID) and, once the session or MCP key is resolved, the
`attendee_id`. Never a name, an email or a token: secrets that could reach a
message (sign-in links, MCP keys, bearer headers) are redacted on every line,
in both formats.
"""

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import metrics

# One mutable dict per request: set by RequestContextMiddleware before the app
# runs, so code that runs in copied contexts (FastAPI, the MCP transport) and
# uvicorn's access log after it all see what a handler filled in.
_request: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("hallway_request", default=None)

_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def new_request(headers: dict[str, str]) -> dict[str, str]:
    """A context for one request; an id the client or Cloudflare sent is kept when it's sane."""
    sent = headers.get("x-request-id") or headers.get("cf-ray") or ""
    ctx = {"request_id": sent if _REQUEST_ID.fullmatch(sent) else uuid.uuid4().hex[:16]}
    _request.set(ctx)
    return ctx


def current() -> dict[str, str]:
    return _request.get() or {}


def set_attendee(attendee_id: str | None) -> None:
    """Tag this request's log lines with the attendee it acts for (an opaque id)."""
    ctx = _request.get()
    if ctx is not None and attendee_id:
        ctx["attendee_id"] = attendee_id


# ------------------------------------------------------------------ redaction
SECRETS = (
    (re.compile(r"([?&](?:t|key)=)[^&\s\"']+"), r"\1[redacted]"),  # sign-in links, MCP ?key=
    (re.compile(r"(?i)(\bbearer\s+)[^\s\"',]+"), r"\1[redacted]"),  # Authorization headers
    (re.compile(r"\bhwp?_[A-Za-z0-9_-]{16,}"), "[redacted]"),  # MCP keys and day pass run tokens
)


def redact(text: str) -> str:
    for pattern, repl in SECRETS:
        text = pattern.sub(repl, text)
    return text


class RedactSecrets(logging.Filter):
    """Redacts secrets from a record in place. The formatted message is what's
    checked (a token can be in the arguments, or split across message and
    arguments: "Bearer %s"), and replaces message + arguments when anything
    was found. uvicorn's access records keep their argument tuple, which its
    formatter unpacks, so those are redacted argument by argument."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            return True
        try:
            message = record.getMessage()
        except Exception:  # a malformed record: logging reports it, not us
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


class RequestContext(logging.Filter):
    """Puts request_id / attendee_id on every record (empty outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current()
        record.request_id = ctx.get("request_id", "")
        record.attendee_id = ctx.get("attendee_id", "")
        return True


# ------------------------------------------------------------------ formats
class JSONFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, request_id, attendee_id,
    exc (a traceback), and for access lines method, path, status, client."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "attendee_id"):
            if getattr(record, key, ""):
                out[key] = getattr(record, key)
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, _version, status = record.args
            out.update(client=client, method=method, path=path, status=status)
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str, ensure_ascii=False)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
TEXT_FORMAT_REQ = "%(asctime)s %(levelname)s %(name)s [%(request_id)s%(attendee_tag)s]: %(message)s"


class TextFormatter(logging.Formatter):
    """The plain format, with [request_id attendee=id] when inside a request."""

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", "")
        aid = getattr(record, "attendee_id", "")
        record.attendee_tag = f" attendee={aid}" if aid else ""
        self._style._fmt = TEXT_FORMAT_REQ if rid else TEXT_FORMAT
        return super().format(record)


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def setup_logging(fmt: str = "text", level: int = logging.INFO) -> logging.Handler:
    """One stderr handler on the root logger with the chosen format, the
    request context and redaction. In JSON mode uvicorn's loggers are routed
    through it too (uvicorn configured them before importing the app); in text
    mode they keep uvicorn's own format, plus redaction. Returns the handler."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RequestContext())
    handler.addFilter(RedactSecrets())
    root = logging.getLogger()
    # Ours from an earlier call, and the bare "%(message)s" console handler the
    # MCP SDK's FastMCP() installs with basicConfig at import: it would print
    # every line a second time, unformatted and unredacted. (Subclasses, such
    # as pytest's capture handlers, stay.)
    for h in list(root.handlers):
        if getattr(h, "_hallway", False) or type(h) is logging.StreamHandler or type(h).__name__ == "RichHandler":
            root.removeHandler(h)
    handler._hallway = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactSecrets) for f in lg.filters):
            lg.addFilter(RedactSecrets())  # records logged on it directly, whatever handler prints them
        if fmt == "json":
            lg.handlers = []
            lg.propagate = True
    return handler


# ------------------------------------------------------------------ requests
def route_label(scope: Scope) -> str:
    """The route template ("/api/meetings/{meeting_id}"), never the raw path,
    so metrics have one series per route, not per id."""
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        return "/mcp"
    if path.startswith("/static/"):
        return "/static"
    route_path = getattr(scope.get("route"), "path", None)
    return route_path if isinstance(route_path, str) and route_path else "unmatched"


class RequestMiddleware:
    """Outermost: gives each request its context (request id, later the
    attendee), echoes X-Request-ID, and records count and latency per route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = new_request(headers)["request_id"]
        started = time.perf_counter()
        status = 500  # unless a response starts

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route, method = route_label(scope), scope.get("method", "")
            metrics.http_requests.inc(method=method, route=route, status=str(status))
            metrics.http_latency.observe(time.perf_counter() - started, method=method, route=route)
