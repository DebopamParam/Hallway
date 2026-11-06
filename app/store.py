"""SQLite persistence. Small and synchronous on purpose: one event, <=1000 people."""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS attendees (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL,
  handle TEXT UNIQUE NOT NULL,
  agent_name TEXT UNIQUE NOT NULL,
  mode TEXT NOT NULL DEFAULT 'connect',
  profile TEXT NOT NULL DEFAULT '{}',
  contact TEXT NOT NULL DEFAULT '{}',
  autonomy TEXT NOT NULL DEFAULT '{}',
  mcp_token TEXT UNIQUE NOT NULL,
  session_token TEXT UNIQUE NOT NULL,
  mm_user_id TEXT,
  mm_password TEXT,
  mm_token TEXT,
  agent_mm_user_id TEXT,
  switch_agent_id TEXT,
  switch_agent_key TEXT,
  cursors TEXT NOT NULL DEFAULT '{}',
  privacy TEXT NOT NULL DEFAULT '{}',
  agent_seen_at REAL,
  is_persona INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  mm_channel_id TEXT,
  created_by TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  attendee_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL,
  decided_at REAL
);
CREATE TABLE IF NOT EXISTS meetings (
  id TEXT PRIMARY KEY,
  proposer_id TEXT NOT NULL,
  invitee_id TEXT NOT NULL,
  time TEXT NOT NULL,
  place TEXT NOT NULL,
  why TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed',
  room_id TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY,
  requester_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  requester_ok INTEGER NOT NULL DEFAULT 0,
  target_ok INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attendee_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  agent_seen INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS guard_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id TEXT,
  role TEXT,
  outcome TEXT NOT NULL,
  categories TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS passes (
  id TEXT PRIMARY KEY,
  attendee_id TEXT UNIQUE NOT NULL,
  cap_usd REAL NOT NULL,
  spent_usd REAL NOT NULL DEFAULT 0,
  runs INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  last_run_at REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pass_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attendee_id TEXT NOT NULL,
  engine TEXT NOT NULL,
  cost_usd REAL NOT NULL,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  ok INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JSON_COLS = {"profile", "contact", "autonomy", "cursors", "privacy", "payload"}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, path: str = settings.db_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLS & d.keys():
            d[k] = json.loads(d[k]) if d[k] else {}
        return d

    def one(self, sql: str, *args: Any) -> dict | None:
        return self._row(self.db.execute(sql, args).fetchone())

    def all(self, sql: str, *args: Any) -> list[dict]:
        return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    def insert(self, table: str, **values: Any) -> None:
        values = {k: json.dumps(v) if k in JSON_COLS else v for k, v in values.items()}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))

    def update(self, table: str, key: str, **values: Any) -> None:
        values = {k: json.dumps(v) if k in JSON_COLS else v for k, v in values.items()}
        sets = ", ".join(f"{k} = ?" for k in values)
        self.db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), key))

    def kv_get(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # convenience
    def attendee(self, **where: Any) -> dict | None:
        (k, v), = where.items()
        return self.one(f"SELECT * FROM attendees WHERE {k} = ?", v)

    def attendees(self) -> list[dict]:
        return self.all("SELECT * FROM attendees ORDER BY created_at")

    def notice(self, attendee_id: str, kind: str, text: str, audience: str = "both", **payload: Any) -> None:
        self.insert(
            "notices", attendee_id=attendee_id, audience=audience, kind=kind, text=text,
            payload=payload, created_at=time.time(),
        )
