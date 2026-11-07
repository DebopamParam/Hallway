"""SQLite persistence. Small and synchronous on purpose: one event, <=1000 people.

Secrets that grant access (session cookies, MCP keys, day pass worker tokens)
are stored as sha256 hashes and looked up by hash; the plain value only ever
lives in the browser cookie or the attendee's agent config."""

import contextlib
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("hallway.store")

BUSY_TIMEOUT_MS = 5000  # wait this long for another connection's write lock (a backup, a migration) before failing

ATTENDEES = """
CREATE TABLE IF NOT EXISTS {name} (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL,
  handle TEXT UNIQUE NOT NULL,
  agent_name TEXT UNIQUE NOT NULL,
  mode TEXT NOT NULL DEFAULT 'connect',
  profile TEXT NOT NULL DEFAULT '{}',
  contact TEXT NOT NULL DEFAULT '{}',
  autonomy TEXT NOT NULL DEFAULT '{}',
  mcp_token_hash TEXT UNIQUE NOT NULL,
  mcp_token_hint TEXT NOT NULL DEFAULT '',
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
"""

# The tables as migration 1 creates them. Frozen: a later change to the layout
# is a new migration at the end of MIGRATIONS, never an edit here, so every DB
# goes through the same steps whatever version it starts from.
BASE_SCHEMA = (
    ATTENDEES.replace("{name}", "attendees")
    + """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  attendee_id TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_attendee ON sessions (attendee_id);
CREATE TABLE IF NOT EXISTS worker_tokens (
  id TEXT PRIMARY KEY,
  attendee_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
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
)

ACTOR_KINDS = ("person", "agent", "host", "system")


@dataclass(frozen=True)
class Actor:
    """Who a notice is about. For an agent, `handle` is the agent's name and
    `name` its person's display name ("Ana Ruiz", shown as "Ana Ruiz's agent")."""

    kind: str  # one of ACTOR_KINDS
    id: str | None = None
    handle: str | None = None
    name: str | None = None

    @classmethod
    def person(cls, a: dict) -> "Actor":
        return cls("person", a["id"], a["handle"], a["name"])

    @classmethod
    def agent(cls, a: dict) -> "Actor":
        return cls("agent", a["id"], a["agent_name"], a["name"])


SYSTEM = Actor("system")

JSON_COLS = {"profile", "contact", "autonomy", "cursors", "privacy", "payload"}


SESSION_TOUCH_S = 300  # slide a session's expiry at most this often, not on every request
WORKER_TOKEN_TTL_S = 3600  # a day pass run token outlives the longest run (the worker's 600 s timeout)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_hint(token: str) -> str:
    """What the app may show of an MCP key once the key itself is gone: its first characters."""
    return token[:7] + "…"


def new_mcp_token() -> str:
    return "hw_" + secrets.token_urlsafe(24)


# The private contact vault: the only fields the app offers. Each value must
# look like what it says, so the guard (which redacts every attendee's values
# in every message) can't be fed ordinary words or unbounded numbers of values.
CONTACT_FIELDS = ("email", "phone", "linkedin")
_CONTACT_SHAPE = {
    "email": re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+"),
    "phone": re.compile(r"\+?[\d\s().-]+"),
    "linkedin": re.compile(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", re.IGNORECASE),
}


def contact_value_ok(field: str, value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 200 or field not in _CONTACT_SHAPE:
        return False
    v = value.strip()
    if field == "phone" and not 7 <= len(re.sub(r"\D", "", v)) <= 15:
        return False
    return bool(_CONTACT_SHAPE[field].fullmatch(v))


def contact_private_values(contact: dict | None) -> list[str]:
    """The strings in an attendee's private contact fields that the guard must
    never let through: each value of 5+ characters, and for a phone also its
    last 10 digits (so "+91 98111 22233" also catches "9811122233"). Only the
    vault's fields, shaped as contact_value_ok says, count, even in a row
    stored before those were checked."""
    vals: list[str] = []
    for k in CONTACT_FIELDS:
        v = (contact or {}).get(k) if isinstance(contact, dict) else None
        if isinstance(v, str) and len(v.strip()) >= 5 and contact_value_ok(k, v):
            vals.append(v.strip())
            digits = re.sub(r"\D", "", v)
            if len(digits) >= 8:
                vals.append(digits[-10:])
    return vals


def configure(db: sqlite3.Connection) -> None:
    """Per-connection settings. WAL lets the app read while a backup or another
    reader holds the file and survives a crash mid-write; NORMAL sync is safe
    with WAL (a power cut can lose the last commits, never corrupt the DB)."""
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA foreign_keys = ON")  # per connection; enforced for any table that declares REFERENCES


def notice_view(n: dict) -> dict:
    """A notices row as the API shows it. `actor` is null for system notices;
    `subject` holds only the ids that are set."""
    actor = None
    if n.get("actor_kind", "system") != "system":
        actor = {"kind": n["actor_kind"], "handle": n.get("actor_handle"), "name": n.get("actor_name")}
    subject = {
        k: v
        for k, v in (
            ("room_id", n.get("room_id")),
            ("room", n.get("room_name")),
            ("meeting_id", n.get("meeting_id")),
            ("approval_id", n.get("approval_id")),
        )
        if v
    }
    return {
        "id": n["id"],
        "kind": n["kind"],
        "text": n["text"],
        "structured": bool(n.get("structured")),
        "actor": actor,
        "subject": subject,
        "payload": n.get("payload") or {},
        "created_at": n["created_at"],
    }


# ------------------------------------------------------------------ migrations
# MIGRATIONS[i] takes a DB from PRAGMA user_version i to i + 1. Each runs in
# one transaction together with the version bump, so a crash leaves the DB at
# the version before it, and the next start runs it again. Append only.


def _m1_base_schema(db: sqlite3.Connection) -> None:
    """0 -> 1: the tables. On a DB from before versioning (user_version 0 but
    the old `attendees` with plain tokens) this only adds what's missing."""
    for stmt in BASE_SCHEMA.split(";"):
        if stmt.strip():
            db.execute(stmt)


def _m2_hash_tokens(db: sqlite3.Connection) -> None:
    """1 -> 2: each attendee's lifelong plain `session_token` becomes a hashed
    row in `sessions` (a normal 7-day session from now, so nobody is signed
    out), the plain `mcp_token` becomes `mcp_token_hash` + a hint, and
    onboarding `pre:<token>` keys become `pre:<hash>`. SQLite can't drop a
    UNIQUE column, so the attendees table is rebuilt without the plain ones.
    Nothing to do on a DB created with hashed tokens."""
    old_cols = {r[1] for r in db.execute("PRAGMA table_info(attendees)")}
    if "session_token" not in old_cols:
        return
    now = time.time()
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, attendee_id, user_agent, created_at, last_seen_at, expires_at) "
        "SELECT sha256_hex(session_token), id, '', ?, ?, ? FROM attendees WHERE session_token IS NOT NULL",
        (now, now, now + settings.session_ttl_s),
    )
    db.execute("DROP TABLE IF EXISTS attendees_v2")
    db.execute(ATTENDEES.replace("{name}", "attendees_v2").strip().rstrip(";"))
    keep = [c for c in (r[1] for r in db.execute("PRAGMA table_info(attendees_v2)")) if c in old_cols]
    cols = ", ".join(keep)
    db.execute(
        f"INSERT INTO attendees_v2 ({cols}, mcp_token_hash, mcp_token_hint) "
        f"SELECT {cols}, sha256_hex(mcp_token), substr(mcp_token, 1, 7) || '…' FROM attendees"
    )
    db.execute("DROP TABLE attendees")
    db.execute("ALTER TABLE attendees_v2 RENAME TO attendees")
    for key, value in db.execute("SELECT key, value FROM kv WHERE key LIKE 'pre:%'").fetchall():
        db.execute("DELETE FROM kv WHERE key = ?", (key,))
        db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", ("pre:" + token_hash(key[4:]), value))


def _m3_indexes(db: sqlite3.Connection) -> None:
    """2 -> 3: indexes for the per-attendee lookups made on every request
    (inbox, notices, approvals, meetings) and the day pass ledger."""
    for stmt in (
        "CREATE INDEX IF NOT EXISTS notices_attendee ON notices (attendee_id, agent_seen)",
        "CREATE INDEX IF NOT EXISTS approvals_attendee ON approvals (attendee_id, status)",
        "CREATE INDEX IF NOT EXISTS meetings_proposer ON meetings (proposer_id)",
        "CREATE INDEX IF NOT EXISTS meetings_invitee ON meetings (invitee_id)",
        "CREATE INDEX IF NOT EXISTS meetings_room ON meetings (room_id)",
        "CREATE INDEX IF NOT EXISTS pass_runs_attendee ON pass_runs (attendee_id)",
        "CREATE INDEX IF NOT EXISTS worker_tokens_attendee ON worker_tokens (attendee_id)",
    ):
        db.execute(stmt)


NOTICE_COLUMNS = (
    # who the notice is about: a person, their agent, the host (Hallway) or the system
    ("actor_kind", "TEXT NOT NULL DEFAULT 'system'"),
    ("actor_id", "TEXT"),  # attendee id, when the actor is an attendee or their agent
    ("actor_handle", "TEXT"),  # their handle (person) or agent name (agent), as it was then
    ("actor_name", "TEXT"),  # the person's display name, as it was then
    # what it is about
    ("room_id", "TEXT"),
    ("meeting_id", "TEXT"),
    ("approval_id", "TEXT"),
    # 1: kind/actor/subject/payload carry everything the app shows; 0: an old
    # row only its `text` can describe
    ("structured", "INTEGER NOT NULL DEFAULT 0"),
)

# Old notice text, for _m4: the wording Hallway used before notices were structured.
_OLD_TEXT = {
    "mention": re.compile(r"^(?P<who>.+?) → your agent: (?P<body>.*)$", re.S),
    "welcome": re.compile(
        r"^Welcome to (?P<event>.+)! (?:Connect your agent from the Connect tab|Set up your agent from the Agent tab)\.$",
        re.S,
    ),
    "room": re.compile(r"^(?P<who>.+?) started a room with you: (?P<title>.*)$", re.S),
    "approval": re.compile(r"^(?P<who>.+?) (?P<verb>approved|declined): (?P<summary>.*)$", re.S),
    "contact_exchanged": re.compile(r"^Contact details swapped with (?P<who>.+?)\. They're in the app", re.S),
}


def _m4_structured_notices(db: sqlite3.Connection) -> None:
    """3 -> 4: notices get typed fields (actor, subject ids) next to `text`,
    so nothing has to parse the text back. Old rows are converted where their
    wording and the rows they point at allow it; the rest stay `structured = 0`
    and are shown as their plain text."""
    have = {r[1] for r in db.execute("PRAGMA table_info(notices)")}
    for col, decl in NOTICE_COLUMNS:
        if col not in have:
            db.execute(f"ALTER TABLE notices ADD COLUMN {col} {decl}")
    db.execute("CREATE INDEX IF NOT EXISTS notices_meeting ON notices (meeting_id)")
    old_factory, db.row_factory = db.row_factory, sqlite3.Row
    try:
        rows = db.execute("SELECT * FROM notices WHERE structured = 0").fetchall()
        for row in rows:
            fields = _legacy_notice_fields(db, dict(row))
            if fields is None:
                continue
            payload = {**json.loads(row["payload"] or "{}"), **fields.pop("payload", {})}
            fields.update(payload=json.dumps(payload), structured=1)
            sets = ", ".join(f"{k} = ?" for k in fields)
            db.execute(f"UPDATE notices SET {sets} WHERE id = ?", (*fields.values(), row["id"]))
    finally:
        db.row_factory = old_factory


def _legacy_notice_fields(db: sqlite3.Connection, n: dict) -> dict | None:
    """The structured fields for one pre-v4 notice, or None to leave it as text."""
    kind, text = n["kind"], n["text"]
    payload = json.loads(n["payload"] or "{}")

    def by_name(name: str) -> sqlite3.Row | None:
        found = db.execute("SELECT id, handle, agent_name, name FROM attendees WHERE name = ?", (name,)).fetchall()
        return found[0] if len(found) == 1 else None

    def by_id(aid: str) -> sqlite3.Row | None:
        r: sqlite3.Row | None = db.execute(
            "SELECT id, handle, agent_name, name FROM attendees WHERE id = ?", (aid,)
        ).fetchone()
        return r

    def person(a: sqlite3.Row | None, name: str) -> dict:
        if a is None:
            return {"actor_kind": "person", "actor_name": name}
        return {"actor_kind": "person", "actor_id": a["id"], "actor_handle": a["handle"], "actor_name": a["name"]}

    def meeting(mid: str | None) -> sqlite3.Row | None:
        r: sqlite3.Row | None = db.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone() if mid else None
        return r

    m = _OLD_TEXT[kind].match(text) if kind in _OLD_TEXT else None
    if kind == "mention" and m:
        who = m["who"]
        if who.endswith(" (agent)"):
            name = who.removesuffix(" (agent)")
            a = by_name(name)
            actor = {"actor_kind": "agent", "actor_name": name}
            if a is not None:
                actor.update(actor_id=a["id"], actor_handle=a["agent_name"])
        else:
            actor = person(by_name(who), who)
        return {**actor, "payload": {"body": m["body"]}}
    if kind == "welcome" and m:
        return {"payload": {"event": m["event"]}}
    if kind == "daypass":
        return {"payload": {"status": "spent"}}
    if kind == "privacy_check" and "passed" in payload:
        return {"payload": {"leak_count": len(payload.get("leaks") or [])}}
    if kind == "room" and m:
        room = db.execute("SELECT id FROM rooms WHERE name = ?", (payload.get("room"),)).fetchone()
        return {
            **person(by_name(m["who"]), m["who"]),
            "room_id": room["id"] if room else None,
            "payload": {"title": m["title"]},
        }
    if kind == "approval" and m:
        return {
            **person(by_name(m["who"]), m["who"]),
            "approval_id": payload.get("approval_id"),
            "payload": {"summary": m["summary"]},
        }
    if kind == "contact_exchanged" and m:
        return person(by_name(m["who"]), m["who"])
    if kind in ("meeting_proposed", "meeting_declined", "meeting_confirmed"):
        mt = meeting(payload.get("meeting_id"))
        if mt is None:
            return None
        proposer, invitee = by_id(mt["proposer_id"]), by_id(mt["invitee_id"])
        if proposer is None or invitee is None:
            return None
        base = {"meeting_id": mt["id"], "payload": {"time": mt["time"], "place": mt["place"]}}
        if kind == "meeting_proposed":
            base["payload"]["why"] = mt["why"]
            return {**person(proposer, proposer["name"]), **base}
        if kind == "meeting_declined":
            return {**person(invitee, invitee["name"]), **base}
        base["payload"].update(proposer_name=proposer["name"], invitee_name=invitee["name"])
        return {**person(invitee, invitee["name"]), "room_id": mt["room_id"], **base}
    return None


def _m5_live_agent_proof(db: sqlite3.Connection) -> None:
    """4 -> 5: `agent_verified`, whether a Live attendee proved their Switch
    agent is theirs (it posted a one-time code). Stand-ins are Hallway's own,
    so verified. Live agents bound before this proved nothing: they start
    unverified and are offered the check."""
    if "agent_verified" not in {r[1] for r in db.execute("PRAGMA table_info(attendees)")}:
        db.execute("ALTER TABLE attendees ADD COLUMN agent_verified INTEGER NOT NULL DEFAULT 1")
    db.execute("UPDATE attendees SET agent_verified = 0 WHERE mode = 'live'")


def _m6_guard_log_about(db: sqlite3.Connection) -> None:
    """5 -> 6: guard_log.about, the attendees whose private values a check
    caught (",id1,id2,"), so each can see what was caught about them and
    nobody else's. Older rows are about nobody."""
    if "about" not in {r[1] for r in db.execute("PRAGMA table_info(guard_log)")}:
        db.execute("ALTER TABLE guard_log ADD COLUMN about TEXT")


def _m7_pass_note(db: sqlite3.Connection) -> None:
    """6 -> 7: passes.note, what a day pass holder asked their agent to do
    ("find me DevOps people"), and note_at, when they last changed it."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(passes)")}
    if "note" not in cols:
        db.execute("ALTER TABLE passes ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "note_at" not in cols:
        db.execute("ALTER TABLE passes ADD COLUMN note_at REAL")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m1_base_schema,
    _m2_hash_tokens,
    _m3_indexes,
    _m4_structured_notices,
    _m5_live_agent_proof,
    _m6_guard_log_about,
    _m7_pass_note,
]
SCHEMA_VERSION = len(MIGRATIONS)


def db_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def migrate(db: sqlite3.Connection, migrations: list[Callable[[sqlite3.Connection], None]] | None = None) -> int:
    """Apply the migrations this DB hasn't had, in order, each in its own
    transaction. Returns the version it ends at. Safe to run on every start
    and from two processes at once (the version is re-read under the lock)."""
    steps = MIGRATIONS if migrations is None else migrations
    for target in range(1, len(steps) + 1):
        if db_version(db) >= target:
            continue
        db.execute("BEGIN IMMEDIATE")
        try:
            if db_version(db) < target:  # another process may have just done it
                steps[target - 1](db)
                db.execute(f"PRAGMA user_version = {target}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        log.info("database migrated to version %d (%s)", target, steps[target - 1].__name__)
    version = db_version(db)
    if version > len(steps):
        log.warning(
            "database is at version %d, newer than this code (%d): was Hallway downgraded?", version, len(steps)
        )
    return version


class Store:
    # The private-values cache is kept up to date by insert()/update(); this
    # full re-read is only a backstop for a write that bypassed them.
    PRIVATE_MAX_AGE_S = 300

    def __init__(self, path: str = settings.db_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit (isolation_level=None): every statement commits on its own,
        # and multi-statement writes that must land together use transaction().
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        configure(self.db)
        self.db.create_function("sha256_hex", 1, lambda t: token_hash(t) if t is not None else None, deterministic=True)
        migrate(self.db)
        self._private: dict[str, list[str]] | None = None  # attendee id -> private contact values
        self._private_read_at = 0.0
        self.private_version = 0  # bumped whenever any attendee's private values may have changed

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises.

        A plain `with`, not `async with`, on purpose: nothing can await inside,
        so no other coroutine's writes on this shared connection slip into it.
        IMMEDIATE takes the write lock up front, so another process (a backup,
        the sqlite3 shell) waits for busy_timeout instead of failing halfway.
        Nested calls join the outer transaction."""
        if self.db.in_transaction:
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def checkpoint(self) -> None:
        """Fold the WAL back into the main file (on shutdown), so a stopped
        Hallway's DB is one self-contained file. Best effort: a reader holding
        the WAL just means it's done on the next start."""
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            log.warning("WAL checkpoint on shutdown: %s", e)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return None if row is None else Store._dict(row)

    @staticmethod
    def _dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in JSON_COLS & d.keys():
            d[k] = json.loads(d[k]) if d[k] else {}
        return d

    def one(self, sql: str, *args: Any) -> dict | None:
        return self._row(self.db.execute(sql, args).fetchone())

    def need(self, sql: str, *args: Any) -> dict:
        """Like one(), for rows the caller knows exist (just written, or held by a foreign key)."""
        row = self.one(sql, *args)
        if row is None:
            raise LookupError(f"no row for {sql!r} {args!r}")
        return row

    def all(self, sql: str, *args: Any) -> list[dict]:
        return [self._dict(r) for r in self.db.execute(sql, args).fetchall()]

    def insert(self, table: str, **values: Any) -> None:
        raw = values
        values = {k: json.dumps(v) if k in JSON_COLS else v for k, v in values.items()}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        if table == "attendees":
            self._contact_written(raw["id"], raw.get("contact"))

    def upsert(self, table: str, update: tuple[str, ...] = (), **values: Any) -> bool:
        """Insert a row keyed by `id`; if one with that id is already there,
        overwrite just the `update` columns (none: leave it as it is). For rows
        whose id comes from outside (a Switch room id) and that two code paths
        may both record. Returns whether a new row was inserted."""
        enc = {k: json.dumps(v) if k in JSON_COLS else v for k, v in values.items()}
        cols = ", ".join(enc)
        marks = ", ".join("?" for _ in enc)
        action = "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in update) if update else "DO NOTHING"
        existed = self.db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (values["id"],)).fetchone() is not None
        self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT(id) {action}", tuple(enc.values()))
        return not existed

    def update(self, table: str, key: str, **values: Any) -> None:
        raw = values
        values = {k: json.dumps(v) if k in JSON_COLS else v for k, v in values.items()}
        sets = ", ".join(f"{k} = ?" for k in values)
        self.db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), key))
        if table == "attendees" and "contact" in raw:
            self._contact_written(key, raw["contact"])

    # ------------------------------------------------- private values cache
    def _contact_written(self, attendee_id: str, contact: dict | None) -> None:
        if self._private is not None:
            self._private[attendee_id] = contact_private_values(contact)
        self.private_version += 1

    def forget_private(self, attendee_id: str) -> None:
        """A removed attendee's private values no longer need guarding."""
        if self._private is not None:
            self._private.pop(attendee_id, None)
        self.private_version += 1

    def private_values(self) -> list[str]:
        """Every attendee's private contact values, for the guard. Cached per
        attendee and updated on each attendee insert / contact update, so the
        guard doesn't scan the attendees table on every message."""
        now = time.monotonic()
        if self._private is None or now - self._private_read_at > self.PRIVATE_MAX_AGE_S:
            fresh = {
                r["id"]: contact_private_values(json.loads(r["contact"] or "{}"))
                for r in self.db.execute("SELECT id, contact FROM attendees")
            }
            if fresh != self._private:
                self.private_version += 1
            self._private, self._private_read_at = fresh, now
        return [v for vals in self._private.values() for v in vals]

    def private_owners(self, texts: list[str]) -> list[str]:
        """The attendees one of whose private values is in one of `texts` (the
        guard's findings)."""
        self.private_values()
        found = [t.lower() for t in texts]
        return sorted(
            aid for aid, vals in (self._private or {}).items() if any(v.lower() in t for v in vals for t in found)
        )

    def kv_get(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def kv_pop(self, key: str) -> Any:
        """Read and delete in one statement: of two concurrent callers, only one gets the value."""
        row = self.db.execute("DELETE FROM kv WHERE key = ? RETURNING value", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def kv_purge(self, prefix: str, older_than: float, field: str = "at") -> None:
        """Drop `prefix*` records whose JSON `field` is before `older_than`."""
        self.db.execute(
            "DELETE FROM kv WHERE key LIKE ? ESCAPE '\\' AND json_extract(value, ?) < ?",
            (prefix.replace("%", r"\%").replace("_", r"\_") + "%", f"$.{field}", older_than),
        )

    # convenience
    def attendee(self, **where: Any) -> dict | None:
        ((k, v),) = where.items()
        return self.one(f"SELECT * FROM attendees WHERE {k} = ?", v)

    def need_attendee(self, **where: Any) -> dict:
        ((k, v),) = where.items()
        return self.need(f"SELECT * FROM attendees WHERE {k} = ?", v)

    def attendees(self) -> list[dict]:
        return self.all("SELECT * FROM attendees ORDER BY created_at")

    def notice(
        self,
        attendee_id: str,
        kind: str,
        text: str,
        *,
        actor: Actor = SYSTEM,
        audience: str = "both",
        room_id: str | None = None,
        meeting_id: str | None = None,
        approval_id: str | None = None,
        **payload: Any,
    ) -> None:
        """Record a notice for an attendee. `text` is the one-line wording (for
        agents, and the fallback); the app renders from kind, actor, subject ids
        and payload, so nothing needs to parse `text`."""
        if actor.kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind {actor.kind!r}")
        self.insert(
            "notices",
            attendee_id=attendee_id,
            audience=audience,
            kind=kind,
            text=text,
            payload=payload,
            actor_kind=actor.kind,
            actor_id=actor.id,
            actor_handle=actor.handle,
            actor_name=actor.name,
            room_id=room_id,
            meeting_id=meeting_id,
            approval_id=approval_id,
            structured=1,
            created_at=time.time(),
        )

    def notices(self, attendee_id: str, where: str = "", *args: Any, limit: int | None = None) -> list[dict]:
        """An attendee's notices, oldest first unless `where` orders them, in the
        shape the web app and agents get: kind, text, actor, subject, payload."""
        sql = (
            "SELECT n.*, r.name AS room_name FROM notices n LEFT JOIN rooms r ON r.id = n.room_id "
            f"WHERE n.attendee_id = ? {where}" + (f" LIMIT {int(limit)}" if limit else "")
        )
        return [notice_view(n) for n in self.all(sql, attendee_id, *args)]

    # ------------------------------------------------------------- sessions
    def create_session(self, attendee_id: str, user_agent: str = "") -> str:
        """A new browser session: returns the cookie value; only its hash is kept."""
        now = time.time()
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        token = secrets.token_urlsafe(32)
        self.insert(
            "sessions",
            id=token_hash(token),
            attendee_id=attendee_id,
            user_agent=(user_agent or "")[:200],
            created_at=now,
            last_seen_at=now,
            expires_at=min(now + settings.session_ttl_s, now + settings.session_max_s),
        )
        return token

    def session(self, token: str) -> dict | None:
        """The live session for a cookie value, or None. Use slides the expiry
        (written at most every SESSION_TOUCH_S) up to the absolute cap."""
        sid = token_hash(token)
        s = self.one("SELECT * FROM sessions WHERE id = ?", sid)
        now = time.time()
        if not s:
            return None
        if s["expires_at"] <= now:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            return None
        if now - s["last_seen_at"] >= SESSION_TOUCH_S:
            s["last_seen_at"], s["expires_at"] = (
                now,
                min(now + settings.session_ttl_s, s["created_at"] + settings.session_max_s),
            )
            self.db.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                (s["last_seen_at"], s["expires_at"], sid),
            )
        return s

    def revoke_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE id = ?", (token_hash(token),))

    def revoke_sessions(self, attendee_id: str) -> int:
        return self.db.execute("DELETE FROM sessions WHERE attendee_id = ?", (attendee_id,)).rowcount

    # ------------------------------------------------------------ MCP keys
    def set_mcp_token(self, attendee_id: str) -> str:
        """Issue a new MCP key for an attendee, replacing (and so revoking) the old one."""
        token = new_mcp_token()
        self.update("attendees", attendee_id, mcp_token_hash=token_hash(token), mcp_token_hint=token_hint(token))
        return token

    def worker_token(self, attendee_id: str) -> str:
        """A short-lived MCP token for one day pass run, handed to the worker
        only. The attendee's own key is never needed (or recoverable) for it."""
        now = time.time()
        self.db.execute("DELETE FROM worker_tokens WHERE expires_at < ?", (now,))
        token = "hwp_" + secrets.token_urlsafe(24)
        self.insert(
            "worker_tokens",
            id=token_hash(token),
            attendee_id=attendee_id,
            created_at=now,
            expires_at=now + WORKER_TOKEN_TTL_S,
        )
        return token

    def revoke_worker_tokens(self, attendee_id: str) -> None:
        self.db.execute("DELETE FROM worker_tokens WHERE attendee_id = ?", (attendee_id,))

    def mcp_attendee(self, token: str) -> dict | None:
        """The attendee an MCP bearer token belongs to: their own key, or a live day pass run token."""
        h = token_hash(token)
        a = self.attendee(mcp_token_hash=h)
        if a or not token.startswith("hwp_"):
            return a
        w = self.one("SELECT attendee_id FROM worker_tokens WHERE id = ? AND expires_at > ?", h, time.time())
        return self.attendee(id=w["attendee_id"]) if w else None
