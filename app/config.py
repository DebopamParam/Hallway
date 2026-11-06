"""Settings, read once from the environment (and ./.env when present)."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


SIGNIN_MODES = ("dev", "demo", "email")
EMAIL_SENDERS = ("console", "smtp", "resend")


def _signin_mode() -> str:
    """SIGNIN_MODE, or the deprecated DEV_MAGIC_LINKS flag (true -> dev, false
    -> email). Neither set: dev, which only starts on a localhost URL or with
    HALLWAY_ENV=dev (see check_signin)."""
    mode = os.environ.get("SIGNIN_MODE", "").strip().lower()
    if mode:
        return mode
    if "DEV_MAGIC_LINKS" in os.environ:
        return "dev" if _bool("DEV_MAGIC_LINKS", True) else "email"
    return "dev"


@dataclass(frozen=True)
class Settings:
    switch_url: str = os.environ.get("SWITCH_URL", "http://localhost:8000")
    switch_registration_key: str = os.environ.get("SWITCH_REGISTRATION_KEY", "")
    gateway_admin_email: str = os.environ.get("GATEWAY_ADMIN_EMAIL", "")
    gateway_admin_password: str = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
    mm_url: str = os.environ.get("MM_URL", "http://localhost:8065")
    mm_admin_user: str = os.environ.get("MM_ADMIN_USER", "admin")
    mm_admin_password: str = os.environ.get("MM_ADMIN_PASSWORD", "")
    mm_team: str = os.environ.get("MM_TEAM", "switch")
    db_path: str = os.environ.get("HALLWAY_DB", "./data/hallway.db")
    public_url: str = os.environ.get("HALLWAY_PUBLIC_URL", "http://localhost:8787").rstrip("/")
    # Three separate secrets (see check_keys): GUARD_API_KEY is what Switch sends
    # to /guardrails/check, HALLWAY_ADMIN_KEY is the organiser's x-hallway-admin
    # (admin endpoints, persona seeding), DAYPASS_WORKER_KEY is the day pass worker's.
    guard_api_key: str = os.environ.get("GUARD_API_KEY", "")
    admin_key: str = os.environ.get("HALLWAY_ADMIN_KEY") or os.environ.get("ADMIN_KEY", "")
    event_name: str = os.environ.get("EVENT_NAME", "Hallway Demo Event")
    max_attendees: int = int(os.environ.get("MAX_ATTENDEES", "1000"))
    daypass_enabled: bool = _bool("DAYPASS_ENABLED", False)
    daypass_cap_usd: float = float(os.environ.get("DAYPASS_CAP_USD", "1.00"))
    daypass_price: str = os.environ.get("DAYPASS_PRICE", "₹149")
    daypass_every_s: int = int(os.environ.get("DAYPASS_EVERY_S", "600"))
    daypass_worker_key: str = os.environ.get("DAYPASS_WORKER_KEY", "")
    hallway_env: str = os.environ.get("HALLWAY_ENV", "").strip().lower()
    # How sign-in links reach people: dev (shown on screen, any email), demo
    # (shown on screen for new emails only), email (sent, never shown).
    signin_mode: str = _signin_mode()
    magic_link_ttl_s: int = int(os.environ.get("MAGIC_LINK_TTL_S", "900"))
    # Browser sessions: idle expiry, slid forward on use, and an absolute cap from sign-in.
    session_ttl_s: int = int(os.environ.get("SESSION_TTL_S", str(7 * 86400)))
    session_max_s: int = int(os.environ.get("SESSION_MAX_S", str(30 * 86400)))
    email_sender: str = os.environ.get("EMAIL_SENDER", "console").strip().lower()
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    smtp_from: str = os.environ.get("SMTP_FROM", "")
    smtp_starttls: bool = _bool("SMTP_STARTTLS", True)
    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")
    # Invite-only demo: new emails need /?invite=<code>; at most INVITE_LIMIT
    # people (seeded personas don't count) may join. 0 = no limit.
    invite_code: str = os.environ.get("INVITE_CODE", "").strip()
    invite_limit: int = int(os.environ.get("INVITE_LIMIT", "0") or 0)
    demo_video_url: str = os.environ.get("DEMO_VIDEO_URL", "").strip()
    host_agent_name: str = os.environ.get("HOST_AGENT_NAME", "hallway")
    lobby_room_name: str = os.environ.get("LOBBY_ROOM_NAME", "lobby")


AGENDA = [
    {"time": "10:00", "title": "Keynote: India's agent decade", "room": "Main hall"},
    {"time": "11:30", "title": "Agents in the payments stack", "room": "Hall A"},
    {"time": "13:30", "title": "Shipping LLM apps on a budget", "room": "Hall B"},
    {"time": "15:00", "title": "PQC migration in practice", "room": "Hall B"},
    {"time": "16:30", "title": "Open-source maintainers vs AI slop", "room": "Hall A"},
    {"time": "18:00", "title": "Hallway hour (drinks + demos)", "room": "Courtyard"},
]

def is_dev_env(s: "Settings") -> bool:
    """Local development: a localhost public URL, or HALLWAY_ENV=dev said out loud."""
    host = urlparse(s.public_url).hostname or ""
    return s.hallway_env == "dev" or (urlparse(s.public_url).scheme == "http" and host in ("localhost", "127.0.0.1"))


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
PLACEHOLDER_KEYS = ("", "change-me")


def is_local_url(s: "Settings") -> bool:
    """HALLWAY_PUBLIC_URL points at this machine (HALLWAY_ENV doesn't matter)."""
    return (urlparse(s.public_url).hostname or "") in LOCAL_HOSTS


def usable_key(key: str) -> str:
    """The key, or "" when it's unset or the .env.example placeholder: never accept those."""
    return "" if key.strip() in PLACEHOLDER_KEYS else key


def admin_key(s: "Settings") -> str:
    """HALLWAY_ADMIN_KEY, or GUARD_API_KEY for .env files from before it existed (deprecated)."""
    return usable_key(s.admin_key) or usable_key(s.guard_api_key)


def worker_key(s: "Settings") -> str:
    """DAYPASS_WORKER_KEY, or GUARD_API_KEY for .env files from before it existed (deprecated)."""
    return usable_key(s.daypass_worker_key) or usable_key(s.guard_api_key)


def check_keys(s: "Settings") -> list[str]:
    """Refuse to start a public instance with missing, placeholder or shared
    secrets. On a localhost URL, returns deprecation warnings instead."""
    keys = {"GUARD_API_KEY": s.guard_api_key, "HALLWAY_ADMIN_KEY": s.admin_key, "DAYPASS_WORKER_KEY": s.daypass_worker_key}
    if not is_local_url(s):
        bad = [k for k, v in keys.items() if not usable_key(v)]
        if bad:
            raise RuntimeError(
                f"{', '.join(bad)} must be set to a random secret (not empty or change-me) when HALLWAY_PUBLIC_URL "
                f"is not localhost (it is {s.public_url}). Generate one each with: openssl rand -hex 24")
        seen: dict[str, str] = {}
        for k, v in keys.items():
            if v in seen:
                raise RuntimeError(f"{seen[v]} and {k} must be different secrets: if one leaks, the other must not.")
            seen[v] = k
        return []
    warnings = []
    for name, own in (("HALLWAY_ADMIN_KEY", s.admin_key), ("DAYPASS_WORKER_KEY", s.daypass_worker_key)):
        if not usable_key(own) and usable_key(s.guard_api_key):
            warnings.append(f"{name} is not set: falling back to GUARD_API_KEY. This is deprecated and refused on a "
                            f"public URL; set {name} to its own secret.")
    return warnings


def check_signin(s: "Settings") -> None:
    """Refuse to start with a sign-in setup that would let strangers in."""
    if s.signin_mode not in SIGNIN_MODES:
        raise RuntimeError(f"SIGNIN_MODE must be one of {', '.join(SIGNIN_MODES)} (got {s.signin_mode!r}).")
    if s.email_sender not in EMAIL_SENDERS:
        raise RuntimeError(f"EMAIL_SENDER must be one of {', '.join(EMAIL_SENDERS)} (got {s.email_sender!r}).")
    if s.signin_mode == "dev" and not is_dev_env(s):
        raise RuntimeError(
            f"SIGNIN_MODE=dev (or DEV_MAGIC_LINKS=true) hands out a sign-in link for any email, so it only runs on "
            f"http://localhost or with HALLWAY_ENV=dev. HALLWAY_PUBLIC_URL is {s.public_url}. "
            "For a public demo use SIGNIN_MODE=demo; with an email provider use SIGNIN_MODE=email.")
    if s.signin_mode == "email":
        if s.email_sender == "console" and not is_dev_env(s):
            raise RuntimeError("SIGNIN_MODE=email needs a real sender: set EMAIL_SENDER=smtp or EMAIL_SENDER=resend.")
        if s.email_sender == "smtp" and not (s.smtp_host and s.smtp_from):
            raise RuntimeError("EMAIL_SENDER=smtp needs SMTP_HOST and SMTP_FROM.")
        if s.email_sender == "resend" and not (s.resend_api_key and s.smtp_from):
            raise RuntimeError("EMAIL_SENDER=resend needs RESEND_API_KEY and SMTP_FROM (a verified sender address).")


settings = Settings()
