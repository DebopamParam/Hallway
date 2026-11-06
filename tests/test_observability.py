"""Observability (#16): request ids, JSON logs with no secrets in them,
/metrics (Prometheus text, admin only), and the public /health and /ready."""

import io
import json
import logging
import re

import pytest

from app import main, metrics, obs
from app.ratelimit import limiter, parse

from .conftest import ADMIN_KEY, GUARD_KEY, WORKER_KEY, mcp_call, sign_in

ADMIN = {"x-hallway-admin": ADMIN_KEY}
SAMPLE = re.compile(r'^[a-z_:][a-z0-9_:]*(\{([a-z_]+="[^"\n]*",?)*\})? -?[0-9.e+-]+$|^[a-z_:][a-z0-9_:]* [+-]Inf$')


async def scrape(client) -> str:
    r = await client.get("/metrics", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    return r.text


def value(text: str, name: str, **labels: str) -> float:
    """The sample `name{labels}` (labels in any order), or 0 if absent."""
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        series, _, v = line.rpartition(" ")
        got_name, _, got_labels = series.partition("{")
        found = dict(re.findall(r'([a-z_]+)="([^"]*)"', got_labels))
        if got_name == name and found == labels:
            return float(v)
    return 0.0


# ------------------------------------------------------------- /metrics
async def test_metrics_needs_the_admin_key(client):
    assert (await client.get("/metrics")).status_code == 403
    assert (await client.get("/metrics", headers={"x-hallway-admin": "nope"})).status_code == 403
    assert (await client.get("/metrics", headers={"authorization": "Bearer nope"})).status_code == 403
    assert (await client.get("/metrics", headers={"x-hallway-admin": GUARD_KEY})).status_code == 403
    assert (await client.get("/metrics", headers=ADMIN)).status_code == 200
    assert (await client.get("/metrics", headers={"authorization": f"Bearer {ADMIN_KEY}"})).status_code == 200


async def test_metrics_format_is_valid_prometheus_text(client, signup):
    await signup("Asha Rao")
    text = await scrape(client)
    assert text.endswith("\n")
    families = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert kind in ("counter", "gauge", "histogram") and name not in families
            families.add(name)
        elif not line.startswith("# HELP "):
            assert SAMPLE.match(line), line
    assert {
        "hallway_http_requests_total",
        "hallway_http_request_duration_seconds",
        "hallway_signins_total",
        "hallway_registrations_total",
        "hallway_mcp_tool_calls_total",
        "hallway_rate_limited_total",
        "hallway_guard_checks_total",
        "hallway_guard_check_duration_seconds",
        "hallway_daypass_spend_usd_total",
        "hallway_presence_sockets",
    } <= families


async def test_metrics_count_what_happens(client, signup, hallway, monkeypatch):
    before = await scrape(client)
    pc, asha = await signup("Asha Rao", autonomy={"propose_meeting": "auto"})
    await signup("Sana Qureshi", mode="daypass")
    token = asha["connect"]["token"]

    # MCP: an ok call, an error, a meeting proposal, then one over the rate limit.
    await mcp_call(client, token, "whoami")
    await mcp_call(client, token, "propose_meeting", {"with_who": "ghost", "time": "1", "place": "x", "why": "y"})
    await mcp_call(client, token, "propose_meeting", {"with_who": "sana", "time": "1", "place": "x", "why": "y"})
    monkeypatch.setattr(limiter, "limits", {"mcp_call": parse("1/60")})
    limiter.reset()
    await mcp_call(client, token, "whoami")
    assert "Rate limited" in (await mcp_call(client, token, "whoami"))["error"]
    monkeypatch.setattr(limiter, "limits", {})

    # The guard: one clean message, one with an email in it.
    for text in ("hello", "mail me at someone@example.com"):
        await client.post(
            "/guardrails/check",
            headers={"x-flintai-api-key": GUARD_KEY},
            json={"messages": [{"role": "user", "content": text}]},
        )
    # Day pass spend reported by the worker.
    sana = hallway.store.need_attendee(handle="sana")
    r = await client.post(
        f"/internal/daypass/{sana['id']}/runs",
        headers={"x-hallway-worker": WORKER_KEY},
        json={"engine": "test", "cost_usd": 0.0125, "tool_calls": 3},
    )
    assert r.status_code == 200
    hallway.presence.connected.add(sana["id"])  # as if Switch confirmed the stand-in's socket
    await pc.get("/api/meetings")

    after = await scrape(client)

    def delta(name: str, **labels: str) -> float:
        return value(after, name, **labels) - value(before, name, **labels)

    assert delta("hallway_registrations_total", mode="connect", persona="false") == 1
    assert delta("hallway_registrations_total", mode="daypass", persona="false") == 1
    assert delta("hallway_signins_total", outcome="onboarding") == 2
    assert delta("hallway_signin_links_total", mode="dev") == 2
    assert delta("hallway_mcp_tool_calls_total", tool="whoami", outcome="ok") == 2
    assert delta("hallway_mcp_tool_calls_total", tool="whoami", outcome="rate_limited") == 1
    assert delta("hallway_mcp_tool_calls_total", tool="propose_meeting", outcome="error") == 1
    assert delta("hallway_mcp_tool_calls_total", tool="propose_meeting", outcome="ok") == 1
    assert delta("hallway_mcp_tool_call_duration_seconds_count", tool="whoami") == 3
    assert delta("hallway_rate_limited_total", limit="mcp_call") == 1
    assert delta("hallway_guard_checks_total") == 2
    assert delta("hallway_guard_verdicts_total", outcome="ok") == 1
    assert delta("hallway_guard_verdicts_total", outcome="redacted") == 1
    assert delta("hallway_guard_check_duration_seconds_count") == 2
    assert value(after, "hallway_guard_check_duration_seconds_bucket", le="+Inf") == value(
        after, "hallway_guard_check_duration_seconds_count"
    )
    assert value(after, "hallway_daypass_spend_usd_total") == pytest.approx(0.0125)
    assert value(after, "hallway_daypass_runs_total", ok="true") == 1
    assert value(after, "hallway_daypass_passes", status="active") == 1
    assert value(after, "hallway_presence_sockets", state="connected") == 1
    assert value(after, "hallway_attendees", mode="connect") == 1
    # Routes are templates: one series per route, whatever the ids.
    assert delta("hallway_http_requests_total", method="GET", route="/api/meetings", status="200") == 1
    assert delta("hallway_http_requests_total", method="POST", route="/mcp", status="200") == 5
    assert (
        delta("hallway_http_requests_total", method="POST", route="/internal/daypass/{attendee_id}/runs", status="200")
        == 1
    )
    assert sana["id"] not in after


async def test_unmatched_paths_share_one_series(client):
    for i in range(3):
        await client.get(f"/no/such/page/{i}")
    text = await scrape(client)
    assert value(text, "hallway_http_requests_total", method="GET", route="unmatched", status="404") >= 3
    assert "/no/such" not in text


def test_histogram_buckets_are_cumulative():
    h = metrics.Histogram("t_seconds", "test", buckets=(0.1, 1.0))
    for v in (0.05, 0.5, 5.0):
        h.observe(v, route="/x")
    lines = h.render()
    assert 't_seconds_bucket{route="/x",le="0.1"} 1' in lines
    assert 't_seconds_bucket{route="/x",le="1"} 2' in lines
    assert 't_seconds_bucket{route="/x",le="+Inf"} 3' in lines
    assert 't_seconds_count{route="/x"} 3' in lines


def test_a_broken_collector_does_not_break_the_scrape():
    reg = metrics.Registry()
    reg.counter("ok_total", "fine").inc()
    reg.collected("broken", "boom", lambda: 1 / 0)  # type: ignore[arg-type,return-value]
    text = reg.render()
    assert "ok_total 1" in text and "# broken unavailable: ZeroDivisionError" in text


# ------------------------------------------------------- /health, /ready
async def test_health_is_public_and_says_nothing(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_ready_checks_db_and_switch(client, world, monkeypatch):
    monkeypatch.setattr(main, "READY_CACHE_S", 0)
    r = await client.get("/ready")
    assert r.status_code == 200 and r.json() == {"ok": True, "checks": {"db": "ok", "switch": "ok", "started": "ok"}}
    world.switch_down = True
    r = await client.get("/ready")
    assert r.status_code == 503 and r.json()["checks"]["switch"] == "error"
    assert "switch is down" not in r.text  # the reason goes to the log only


async def test_ready_reports_a_broken_db(client, hallway, monkeypatch):
    monkeypatch.setattr(main, "READY_CACHE_S", 0)

    real = hallway.store.db

    class Broken:
        def execute(self, *_a):
            import sqlite3

            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            real.close()

    monkeypatch.setattr(hallway.store, "db", Broken())
    r = await client.get("/ready")
    assert r.status_code == 503 and r.json()["checks"]["db"] == "error"


async def test_ready_is_cached_so_it_cannot_hammer_switch(client, world, monkeypatch):
    monkeypatch.setattr(main, "_ready_cache", (0.0, {}))
    calls = []
    health = type(main.hallway.switch).health

    async def counted(self):
        calls.append(1)
        await health(self)

    monkeypatch.setattr(type(main.hallway.switch), "health", counted)
    for _ in range(5):
        assert (await client.get("/ready")).status_code == 200
    assert len(calls) == 1


# --------------------------------------------------------- request ids
async def test_request_id_is_echoed_or_made(client):
    r = await client.get("/health", headers={"x-request-id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = await client.get("/health", headers={"cf-ray": "8f1e2d3c4b5a6978-BOM"})
    assert r.headers["x-request-id"] == "8f1e2d3c4b5a6978-BOM"
    r = await client.get("/health", headers={"x-request-id": "bad id\nwith newline" + "x" * 80})
    assert re.fullmatch(r"[0-9a-f]{16}", r.headers["x-request-id"])
    a, b = (await client.get("/health")).headers["x-request-id"], (await client.get("/health")).headers["x-request-id"]
    assert a != b


# ---------------------------------------------------------------- logs
@pytest.fixture
def json_logs():
    """LOG_FORMAT=json for one test; yields a function returning the parsed lines."""
    handler = obs.setup_logging("json", logging.DEBUG)
    buf = io.StringIO()
    handler.setStream(buf)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    obs.setup_logging("text")
    for name in obs.UVICORN_LOGGERS:
        logging.getLogger(name).propagate = True


async def test_json_logs_carry_request_and_attendee_and_no_secrets(make_client, signup, json_logs):
    c = make_client()
    r = await c.post("/api/auth/start", json={"email": "asha.rao@example.com"}, headers={"x-request-id": "req-signin"})
    link = r.json()["dev_link"]
    await c.get(link.replace("http://testserver", ""))
    reg = await c.post("/api/register", json={"name": "Asha Rao", "profile": {}, "contact": {}, "autonomy": {}})
    key = reg.json()["connect"]["token"]
    rotated = await c.post("/api/me/agent-key", headers={"x-request-id": "req-rotate"})
    new_key = rotated.json()["connect"]["token"]
    await mcp_call(c, new_key, "whoami", auth="query")
    logging.getLogger("hallway").warning("an agent sent Authorization: Bearer %s", new_key)

    lines = json_logs()
    assert lines and all({"ts", "level", "logger", "msg"} <= set(line) for line in lines)
    text = json.dumps(lines)
    for secret in (link.split("t=")[1], key, new_key, "asha.rao@example.com"):
        assert secret not in text
    issued = next(line for line in lines if "sign-in link issued" in line["msg"])
    assert issued["request_id"] == "req-signin" and "attendee_id" not in issued
    aid = main.hallway.store.need_attendee(handle="asha")["id"]
    rot = next(line for line in lines if "agent key rotated" in line["msg"])
    assert rot["request_id"] == "req-rotate" and rot["attendee_id"] == aid
    registered = next(line for line in lines if "registered attendee" in line["msg"])
    assert registered["attendee_id"] == aid
    assert any("Bearer [redacted]" in line["msg"] for line in lines)


def test_json_access_log_is_structured_and_redacted(json_logs):
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/auth/magic?t=SECRET123&x=1", "1.1", 303
    )
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "POST", "/mcp?key=hw_abcdefghijklmnopqrstu", "1.1", 200
    )
    first, second = json_logs()
    assert first["logger"] == "uvicorn.access" and first["status"] == 303 and first["method"] == "GET"
    assert first["path"] == "/auth/magic?t=[redacted]&x=1" and "SECRET123" not in json.dumps(first)
    assert second["path"] == "/mcp?key=[redacted]" and "hw_abc" not in json.dumps(second)


def test_exceptions_are_logged_as_one_json_line_without_secrets(json_logs):
    try:
        raise RuntimeError("failed calling /mcp?key=hw_abcdefghijklmnopqrstu")
    except RuntimeError:
        logging.getLogger("hallway").exception("boom")
    [line] = json_logs()
    assert line["level"] == "error" and "RuntimeError" in line["exc"] and "hw_abc" not in line["exc"]


def test_one_console_handler_replaces_the_mcp_sdks():
    """FastMCP() calls logging.basicConfig("%(message)s") at import; Hallway's
    handler replaces it, so each line is printed once, formatted and redacted."""
    root = logging.getLogger()
    sdk = logging.StreamHandler()
    root.addHandler(sdk)
    obs.setup_logging("text")
    consoles = [h for h in root.handlers if type(h) is logging.StreamHandler or getattr(h, "_hallway", False)]
    assert sdk not in root.handlers and len(consoles) == 1


def test_secret_split_across_message_and_args_is_redacted():
    rec = logging.LogRecord("hallway", logging.INFO, "", 0, "header was Bearer %s", ("abc.def",), None)
    obs.RedactSecrets().filter(rec)
    assert rec.getMessage() == "header was Bearer [redacted]"
    rec = logging.LogRecord("hallway", logging.INFO, "", 0, "%d%% done for %s", (50, "asha"), None)
    obs.RedactSecrets().filter(rec)
    assert rec.getMessage() == "50% done for asha"  # untouched when there's nothing to redact


def test_text_logs_stay_the_default_and_show_the_request_id(caplog):
    handler = obs.setup_logging("text")
    buf = io.StringIO()
    handler.setStream(buf)
    log = logging.getLogger("hallway")
    log.info("outside a request")
    obs.new_request({"x-request-id": "r-1"})
    obs.set_attendee("a1b2")
    log.info("inside one, key hw_abcdefghijklmnopqrstu")
    obs._request.set(None)
    out = buf.getvalue().splitlines()
    assert out[0].endswith("INFO hallway: outside a request")
    assert out[1].endswith("INFO hallway [r-1 attendee=a1b2]: inside one, key [redacted]")


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("GET /auth/magic?t=abc&x=1", "GET /auth/magic?t=[redacted]&x=1"),
        ("/mcp?key=hw_zzz", "/mcp?key=[redacted]"),
        ("Authorization: Bearer hw_abcdefghijklmnopqrstu", "Authorization: Bearer [redacted]"),
        ("token hwp_abcdefghijklmnopqrstu done", "token [redacted] done"),
        ("hint hw_abcd… is fine", "hint hw_abcd… is fine"),
    ],
)
def test_redact(text, want):
    assert obs.redact(text) == want


async def test_signed_in_request_ids_reach_the_log_via_sign_in(make_client, json_logs):
    c = make_client()
    await sign_in(c, "bo@example.com")
    assert any(line.get("request_id") for line in json_logs())


# ------------------------------------------------------------ watch.sh
WATCH_SAMPLE = """\
2026-11-06 10:00:00,000 INFO hallway [r1]: sign-in link issued for a***@example.com (dev mode)
2026-11-06 10:00:01,000 ERROR hallway [r2 attendee=a1]: register asha: POST /users: 500
INFO:     1.2.3.4:5 - "GET /api/rooms HTTP/1.1" 200 OK
INFO:     1.2.3.4:5 - "GET /api/rooms HTTP/1.1" 502 Bad Gateway
{"ts": "x", "level": "info", "logger": "hallway", "msg": "fine"}
{"ts": "x", "level": "error", "logger": "hallway", "msg": "guard: check failed, answering BLOCKED"}
{"ts": "x", "level": "info", "logger": "uvicorn.access", "msg": "...", "status": 503}
2026-11-06 10:00:02,000 WARNING hallway: guard: a check took 2300 ms; Switch delivers unchecked after 2 s
2026-11-06 10:00:03,000 INFO hallway.presence: stand-in asha.agent connected
"""


def test_watch_picks_out_problems_and_throttles_pushes(tmp_path):
    import http.server
    import subprocess
    import threading
    from pathlib import Path

    pushed: list[str] = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            pushed.append(self.rfile.read(int(self.headers["content-length"])).decode())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = Path(__file__).resolve().parent.parent / "scripts" / "watch.sh"
    url = f"http://127.0.0.1:{server.server_port}/alerts"
    r = subprocess.run(
        ["bash", str(script), "--stdin", "--notify", url], input=WATCH_SAMPLE, capture_output=True, text=True
    )
    server.shutdown()
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert len(out) == 5, out
    assert "register asha" in out[0] and "502 Bad Gateway" in out[1] and "check failed" in out[2]
    assert '"status": 503' in out[3] and "a check took 2300 ms" in out[4]
    assert len(pushed) == 1 and pushed[0].startswith("Hallway: ") and "register asha" in pushed[0]
    quiet = subprocess.run(["bash", str(script), "--stdin"], input="all fine\n", capture_output=True, text=True)
    assert quiet.returncode == 0 and quiet.stdout == ""
