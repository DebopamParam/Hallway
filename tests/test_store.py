"""The SQLite layer (#15): connection settings, transactions, numbered
migrations from any version to the latest, and online backup + restore with
scripts/backup.sh."""

import logging
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import store as store_mod
from app.store import MIGRATIONS, SCHEMA_VERSION, Store, db_version, migrate

ROOT = Path(__file__).resolve().parent.parent


def schema(db: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in db.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def raw_db(path: Path, version: int) -> sqlite3.Connection:
    """A DB as code from `version` left it: the first `version` migrations only."""
    db = sqlite3.connect(path, isolation_level=None)
    db.create_function("sha256_hex", 1, store_mod.token_hash)
    migrate(db, MIGRATIONS[:version])
    assert db_version(db) == version
    return db


# ------------------------------------------------------------ connection
def test_connection_settings(tmp_path):
    s = Store(str(tmp_path / "h.db"))
    pragma = lambda name: s.db.execute(f"PRAGMA {name}").fetchone()[0]  # noqa: E731
    assert pragma("journal_mode") == "wal"
    assert pragma("foreign_keys") == 1
    assert pragma("busy_timeout") == store_mod.BUSY_TIMEOUT_MS
    assert pragma("synchronous") == 1  # NORMAL
    s.db.close()


def test_a_write_waits_for_another_connections_lock(tmp_path):
    """busy_timeout: a backup or the sqlite3 shell holding the write lock for a
    moment delays a write instead of failing it with "database is locked"."""
    path = str(tmp_path / "h.db")
    s = Store(path)
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    threading.Timer(0.3, lambda: other.execute("COMMIT")).start()
    t = time.monotonic()
    s.kv_set("k", 1)
    assert time.monotonic() - t >= 0.25 and s.kv_get("k") == 1
    other.close()
    s.db.close()


def test_transaction_commits_or_rolls_back_together(tmp_path):
    s = Store(str(tmp_path / "h.db"))
    with s.transaction():
        s.kv_set("a", 1)
        with s.transaction():  # nested: joins the outer one
            s.kv_set("b", 2)
    assert (s.kv_get("a"), s.kv_get("b")) == (1, 2)
    with pytest.raises(RuntimeError), s.transaction():
        s.kv_set("a", 10)
        s.kv_set("c", 3)
        raise RuntimeError("halfway")
    assert (s.kv_get("a"), s.kv_get("c")) == (1, None)
    assert not s.db.in_transaction
    s.db.close()


def test_checkpoint_leaves_one_self_contained_file(tmp_path):
    path = tmp_path / "h.db"
    s = Store(str(path))
    s.kv_set("k", "v")
    s.checkpoint()
    assert (tmp_path / "h.db-wal").stat().st_size == 0
    plain = tmp_path / "copy.db"
    plain.write_bytes(path.read_bytes())  # a naive file copy now has everything
    assert sqlite3.connect(plain).execute("SELECT value FROM kv WHERE key = 'k'").fetchone() == ('"v"',)
    s.db.close()


# ------------------------------------------------------------ migrations
def test_migrations_are_numbered_from_one():
    assert SCHEMA_VERSION == len(MIGRATIONS) >= 3
    assert [m.__name__.split("_")[1] for m in MIGRATIONS] == [f"m{i}" for i in range(1, len(MIGRATIONS) + 1)]


def test_v0_db_upgrades_to_latest(tmp_path):
    path = tmp_path / "v0.db"
    sqlite3.connect(path).close()  # an empty file: user_version 0
    s = Store(str(path))
    assert db_version(s.db) == SCHEMA_VERSION
    fresh = Store(str(tmp_path / "fresh.db"))
    assert schema(s.db) == schema(fresh.db)
    assert ("index", "notices_attendee") in schema(s.db)
    s.db.close()
    fresh.db.close()


@pytest.mark.parametrize("version", range(SCHEMA_VERSION + 1))
def test_every_version_upgrades_to_latest_and_keeps_its_data(tmp_path, version):
    path = tmp_path / f"v{version}.db"
    db = raw_db(path, version)
    if version >= 1:
        db.execute(
            "INSERT INTO attendees (id, name, email, handle, agent_name, mcp_token_hash, created_at) "
            "VALUES ('a1', 'Asha', 'asha@example.com', 'asha', 'asha.agent', 'h', 1)"
        )
        db.execute(
            "INSERT INTO notices (attendee_id, audience, kind, text, created_at) VALUES ('a1', 'both', 'k', 't', 1)"
        )
    db.close()

    s = Store(str(path))
    assert db_version(s.db) == SCHEMA_VERSION
    fresh = Store(str(tmp_path / "fresh.db"))
    assert schema(s.db) == schema(fresh.db)
    if version >= 1:
        assert s.need_attendee(handle="asha")["email"] == "asha@example.com"
        assert len(s.all("SELECT * FROM notices WHERE attendee_id = 'a1'")) == 1
    s.db.close()
    fresh.db.close()


def test_migrate_twice_changes_nothing(tmp_path):
    path = tmp_path / "h.db"
    Store(str(path)).db.close()
    db = raw_db(path, SCHEMA_VERSION)
    before = schema(db)
    assert migrate(db) == SCHEMA_VERSION and schema(db) == before


def test_a_failing_migration_rolls_back_and_keeps_the_version(tmp_path):
    db = raw_db(tmp_path / "h.db", SCHEMA_VERSION)

    def m_broken(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE half_done (x)")
        db.execute("ALTER TABLE attendees ADD COLUMN half TEXT")
        raise sqlite3.OperationalError("disk on fire")

    with pytest.raises(sqlite3.OperationalError):
        migrate(db, [*MIGRATIONS, m_broken])
    assert db_version(db) == SCHEMA_VERSION
    assert ("table", "half_done") not in schema(db)
    assert "half" not in {r[1] for r in db.execute("PRAGMA table_info(attendees)")}
    assert not db.in_transaction

    # Fixed and deployed again: it runs, once.
    def m_fixed(db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE attendees ADD COLUMN half TEXT")

    assert migrate(db, [*MIGRATIONS, m_fixed]) == SCHEMA_VERSION + 1
    assert migrate(db, [*MIGRATIONS, m_fixed]) == SCHEMA_VERSION + 1


def test_a_db_newer_than_the_code_is_left_alone_with_a_warning(tmp_path, caplog):
    path = tmp_path / "h.db"
    Store(str(path)).db.close()
    db = sqlite3.connect(path, isolation_level=None)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with caplog.at_level(logging.WARNING, logger="hallway.store"):
        s = Store(str(path))
    assert db_version(s.db) == SCHEMA_VERSION + 1
    assert "newer than this code" in caplog.text
    s.db.close()


# ------------------------------------------------------------- backups
def backup_sh(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "backup.sh"), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "BACKUP_DIR": "", "BACKUP_KEEP": ""},
        **kw,
    )


def test_online_backup_retention_and_restore(tmp_path):
    live = tmp_path / "data" / "hallway.db"
    out = tmp_path / "backups"
    s = Store(str(live))
    s.insert(
        "attendees",
        id="a1",
        name="Asha",
        email="asha@example.com",
        handle="asha",
        agent_name="asha.agent",
        mcp_token_hash="h",
        created_at=1,
    )
    s.kv_set("before", 1)

    # Taken while the app holds the DB open with uncommitted-to-file WAL pages.
    r = backup_sh("--db", str(live), "--out", str(out), "--keep", "2")
    assert r.returncode == 0, r.stderr
    [first] = sorted(out.glob("hallway-*.db"))
    assert "backed up to" in r.stdout and f"schema version {SCHEMA_VERSION}" in r.stdout
    assert oct(first.stat().st_mode & 0o777) == "0o600"
    copy = sqlite3.connect(first)
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # self-contained, no -wal beside it
    assert copy.execute("SELECT handle FROM attendees").fetchall() == [("asha",)]
    copy.close()

    # Retention: only the newest 2 stay; nothing half-written is left behind.
    for _ in range(2):
        time.sleep(1.05)  # names are per second
        assert backup_sh("--db", str(live), "--out", str(out), "--keep", "2").returncode == 0
    kept = sorted(out.glob("hallway-*.db"))
    assert len(kept) == 2 and first not in kept
    assert not list(out.glob("*.partial"))

    # Things change after the backup...
    s.kv_set("after", 2)
    s.db.execute("DELETE FROM attendees")
    s.db.close()  # restore runs with the app stopped

    r = backup_sh("restore", str(kept[-1]), "--db", str(live), "--out", str(out))
    assert r.returncode == 0, r.stderr
    [safety] = out.glob("pre-restore-*.db")  # the DB as it was, kept in case the restore was a mistake
    assert sqlite3.connect(safety).execute("SELECT count(*) FROM attendees").fetchone()[0] == 0

    back = Store(str(live))
    assert back.need_attendee(handle="asha")["name"] == "Asha"
    assert back.kv_get("before") == 1 and back.kv_get("after") is None
    assert db_version(back.db) == SCHEMA_VERSION
    back.db.close()


def test_restore_refuses_a_bad_file_and_changes_nothing(tmp_path):
    live = tmp_path / "hallway.db"
    s = Store(str(live))
    s.kv_set("keep", "me")
    s.db.close()
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    r = backup_sh("restore", str(junk), "--db", str(live), "--out", str(tmp_path / "b"))
    assert r.returncode != 0 and "nothing changed" in r.stderr
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)")
    assert backup_sh("restore", str(other), "--db", str(live), "--out", str(tmp_path / "b")).returncode != 0
    assert Store(str(live)).kv_get("keep") == "me"


def test_backup_of_a_missing_db_fails_cleanly(tmp_path):
    r = backup_sh("--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "b"))
    assert r.returncode != 0 and "no database" in r.stderr
    assert not (tmp_path / "nope.db").exists()
    assert not list((tmp_path / "b").glob("*"))


def test_relative_paths_are_from_where_it_runs(tmp_path):
    Store(str(tmp_path / "hallway.db")).db.close()
    r = backup_sh("--db", "hallway.db", "--out", "bk", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert len(list((tmp_path / "bk").glob("hallway-*.db"))) == 1
