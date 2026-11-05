"""Settings, read once from the environment (and ./.env when present)."""
import os
from dataclasses import dataclass
from pathlib import Path


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
    dev_magic_links: bool = _bool("DEV_MAGIC_LINKS", True)
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

settings = Settings()
