"""The organiser's provider keys and day pass model, set from the dashboard
(app/organiser.py) and kept in one JSON file only its owner can read (0600),
next to the database: HALLWAY_PROVIDERS_FILE, or providers.json beside HALLWAY_DB.

A value here wins over the same name in the environment (.env); clearing it
falls back to the environment again. Readers call get() per use, so a change
takes effect without a restart: research lookups (EXA_API_KEY) here, and the
day pass worker (daypass/worker.py), which reads the same file before each
batch of runs. Values never leave this module except through get(); the
dashboard only ever sees status(): set or not, where from, and the last 4.
"""

import contextlib
import json
import logging
import os
from pathlib import Path

from .config import settings

log = logging.getLogger("hallway")

# name -> what it's for. The keys the code actually reads: the day pass model's
# (PydanticAI reads them from the environment) and Exa's for research_people.
KEYS = {
    "ANTHROPIC_API_KEY": "Anthropic (day pass model, claude-*)",
    "VERCEL_AI_GATEWAY_API_KEY": "Vercel AI Gateway (day pass model, vercel:*)",
    "EXA_API_KEY": "Exa (research_people web lookups)",
}
# Day pass worker settings that aren't secret, kept alongside so the worker reads one file.
OPTIONS = {
    "DAYPASS_MODEL": "claude-haiku-5-5",
    "DAYPASS_PRICE_PER_MTOK": "",
}
KEY_MAX = 400


def path() -> Path:
    explicit = os.environ.get("HALLWAY_PROVIDERS_FILE", "").strip()
    return Path(explicit) if explicit else Path(settings.db_path).parent / "providers.json"


def _read() -> dict[str, str]:
    p = path()
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("providers: can't read %s; using the environment", p)
        return {}
    return {k: v for k, v in data.items() if k in KEYS | OPTIONS and isinstance(v, str) and v}


def _write(values: dict[str, str]) -> None:
    """Atomic, and 0600 from the first byte: the temp file is created with that mode."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()  # a leftover from a crash: start fresh rather than reuse its mode
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(values, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def saved(name: str) -> str:
    """The dashboard's value, or "" when it has none."""
    return _read().get(name, "")


def get(name: str) -> str:
    """The dashboard's value, else the environment's."""
    return saved(name) or os.environ.get(name, "").strip() or OPTIONS.get(name, "")


def update(values: dict[str, str | None]) -> None:
    """Set each name to its value; "" removes it (back to the environment);
    None leaves it alone."""
    unknown = set(values) - set(KEYS) - set(OPTIONS)
    if unknown:
        raise ValueError(f"unknown setting {sorted(unknown)[0]}")
    current = _read()
    for k, v in values.items():
        if v is None:
            continue
        v = v.strip()
        if v:
            current[k] = v
        else:
            current.pop(k, None)
    _write(current)
    log.info("providers: updated %s", ", ".join(sorted(k for k, v in values.items() if v is not None)) or "nothing")


def _last4(value: str) -> str:
    return value[-4:] if len(value) >= 12 else ""  # a short value's last 4 give too much of it away


def status() -> dict:
    """What the dashboard may show: never a key, only whether it's set, from
    where, and its last 4 characters."""
    saved = _read()
    keys = {}
    for name, label in KEYS.items():
        value = saved.get(name) or os.environ.get(name, "").strip()
        keys[name] = {
            "label": label,
            "set": bool(value),
            "source": "dashboard" if name in saved else "env" if value else None,
            "last4": _last4(value) if value else "",
        }
    options = {name: {"value": get(name), "source": "dashboard" if name in saved else "env"} for name in OPTIONS}
    return {"keys": keys, "options": options, "file": str(path())}
