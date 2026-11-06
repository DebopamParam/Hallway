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
    guard_api_key: str = os.environ.get("GUARD_API_KEY", "")
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
    email_sender: str = os.environ.get("EMAIL_SENDER", "console").strip().lower()
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    smtp_from: str = os.environ.get("SMTP_FROM", "")
    smtp_starttls: bool = _bool("SMTP_STARTTLS", True)
    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")
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
