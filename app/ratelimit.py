"""A small in-memory rate limiter: one token bucket per (limit, key).

In memory and per process: Hallway runs as a single uvicorn process (the
Dockerfile starts one worker), so that is the whole picture. Running several
workers would give each its own buckets, multiplying every limit; a restart
forgets them all. Both are fine for a demo.

Every limit is set by an env var, RATE_LIMIT_<NAME>, as "<count>/<period>":
the period in seconds, or with an s/m/h suffix ("20/60", "6/m", "100/h").
"off" (or 0) turns that limit off; RATE_LIMITS=off turns them all off.
"""
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import config  # noqa: F401  (loads ./.env into the environment first)

# Demo defaults. Conference Wi-Fi puts many attendees behind one IP, so the
# per-IP limits are loose; the per-email, per-agent and per-session ones are tight.
DEFAULTS = {
    "auth_start_ip": "20/60",      # POST /api/auth/start, per client IP
    "auth_start_email": "5/900",   # ... and per email address (no inbox flooding)
    "magic_ip": "30/60",           # GET /auth/magic, per client IP
    "register_ip": "10/60",        # POST /api/register, per client IP
    "session_write": "60/60",      # cookie-authenticated POST/PUT/DELETE on /api/*, per session
    "mcp_call": "120/60",          # every MCP tool call, per agent
    "mcp_outreach": "6/60",        # post, create_room, propose_meeting, request_contact, per agent
    "guard": "1200/60",            # POST /guardrails/check (Switch only), per caller
}
UNITS = {"": 1, "s": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}


@dataclass(frozen=True)
class Limit:
    count: int
    period_s: float

    @property
    def rate(self) -> float:
        return self.count / self.period_s


def parse(spec: str) -> Limit | None:
    """"20/60" -> Limit(20, 60); "off" / "0" -> None (no limit)."""
    spec = spec.strip().lower()
    if spec in ("off", "0", "none", ""):
        return None
    m = re.fullmatch(r"(\d+)\s*/\s*(\d*(?:\.\d+)?)\s*([a-z]*)", spec)
    if not m or m.group(3) not in UNITS:
        raise ValueError(f"bad rate limit {spec!r}: use <count>/<seconds>, e.g. 20/60 or 6/m")
    count, period = int(m.group(1)), float(m.group(2) or 1) * UNITS[m.group(3)]
    if count <= 0 or period <= 0:
        return None
    return Limit(count, period)


def limits_from_env(env=os.environ) -> dict[str, Limit | None]:
    if env.get("RATE_LIMITS", "on").strip().lower() in ("off", "false", "0", "no"):
        return {name: None for name in DEFAULTS}
    return {name: parse(env.get(f"RATE_LIMIT_{name.upper()}", default)) for name, default in DEFAULTS.items()}


class RateLimited(Exception):
    def __init__(self, name: str, retry_after: int):
        super().__init__(f"rate limited ({name}), retry after {retry_after} s")
        self.name = name
        self.retry_after = retry_after


class RateLimiter:
    """Token buckets: each holds up to `count` tokens and refills at count/period
    per second, so a burst of `count` is allowed, then a steady rate."""

    PRUNE_AT = 10_000  # buckets kept before idle (full) ones are dropped

    def __init__(self, limits: dict[str, Limit | None], clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.buckets: dict[tuple[str, str], tuple[float, float]] = {}  # -> (tokens, updated_at)

    def hit(self, name: str, key: str) -> int:
        """Take one token. Returns 0 if allowed, else the seconds to wait."""
        limit = self.limits.get(name)
        if limit is None:
            return 0
        now = self.clock()
        tokens, at = self.buckets.get((name, key), (float(limit.count), now))
        tokens = min(float(limit.count), tokens + (now - at) * limit.rate)
        if tokens >= 1:
            self.buckets[(name, key)] = (tokens - 1, now)
            if len(self.buckets) > self.PRUNE_AT:
                self._prune(now)
            return 0
        self.buckets[(name, key)] = (tokens, now)
        return max(1, math.ceil((1 - tokens) / limit.rate))

    def check(self, name: str, key: str) -> None:
        """hit(), raising RateLimited when over the limit."""
        wait = self.hit(name, key)
        if wait:
            raise RateLimited(name, wait)

    def _prune(self, now: float) -> None:
        """Drop buckets that have refilled completely: they behave as if new."""
        for (name, key), (tokens, at) in list(self.buckets.items()):
            limit = self.limits.get(name)
            if limit is None or tokens + (now - at) * limit.rate >= limit.count:
                del self.buckets[(name, key)]

    def reset(self) -> None:
        self.buckets.clear()


limiter = RateLimiter(limits_from_env())
