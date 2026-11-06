"""Online SQLite backup and restore with the sqlite3 backup API (stdlib only).

    python sqlite_backup.py backup  SRC DST    # DST "-": the backup's bytes on stdout
    python sqlite_backup.py restore SRC DST    # SRC "-": a backup's bytes on stdin
    python sqlite_backup.py check   FILE       # integrity check; prints tables and row counts

scripts/backup.sh runs it on the host, or inside the Hallway container (which
has Python but no sqlite3 shell) with `python -c`, so it must stay one file
with no imports outside the standard library.

The backup API copies a consistent snapshot while Hallway keeps running and
writing (WAL mode): it reads committed pages through SQLite, never the raw
file, so a copy of the -wal file isn't needed and a torn copy can't happen.
Restore goes the same way into the live path, so stale -wal/-shm files are
handled by SQLite instead of by hand.
"""

import os
import sqlite3
import sys

BUSY_TIMEOUT_S = 10


def _open(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)


def _check(db: sqlite3.Connection) -> dict[str, int]:
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"integrity check failed: {result}")
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    if "attendees" not in tables:
        raise SystemExit("not a Hallway database (no attendees table)")
    return {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}


def _existing(path: str) -> str:
    if not os.path.isfile(path):
        raise SystemExit(f"no database at {path}")
    return path


def backup(src: str, dst: str) -> None:
    # Not mode=ro: a read-only open of a WAL database fails when it can't
    # create the -shm file, and this only reads anyway.
    source = _open(_existing(src))
    if dst == "-":
        mem = sqlite3.connect(":memory:")
        source.backup(mem)
        _check(mem)
        data = bytearray(mem.serialize())
        # Header bytes 18-19 say "WAL" (2), copied from the source: make the
        # file a plain rollback-journal DB (1), like the branch below.
        data[18:20] = b"\x01\x01"
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        target = _open(dst)
        source.backup(target)
        # One self-contained file: opening a backup never leaves -wal/-shm beside it.
        target.execute("PRAGMA journal_mode = DELETE")
        _check(target)
        target.close()
    source.close()


def restore(src: str, dst: str) -> None:
    if src == "-":
        backup_db = sqlite3.connect(":memory:")
        backup_db.deserialize(sys.stdin.buffer.read())
    else:
        backup_db = _open(_existing(src))
    counts = _check(backup_db)  # never overwrite the live DB with a bad file
    target = _open(dst)
    backup_db.backup(target)
    target.execute("PRAGMA journal_mode = WAL")
    _check(target)
    target.close()
    print(f"restored {dst}: " + ", ".join(f"{t} {n}" for t, n in counts.items() if n))


def check(path: str) -> None:
    db = _open(_existing(path))
    counts = _check(db)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    print(f"ok: schema version {version}; " + ", ".join(f"{t} {n}" for t, n in counts.items()))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["help"]
    commands = {"backup": (backup, 2), "restore": (restore, 2), "check": (check, 1)}
    if cmd not in commands or len(args) != commands[cmd][1]:
        raise SystemExit(__doc__)
    commands[cmd][0](*args)
