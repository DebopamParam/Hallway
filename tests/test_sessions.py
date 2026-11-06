"""Sessions and keys at rest (#8): a new hashed session per sign-in, logout and
"sign out everywhere", sliding + absolute expiry, MCP keys stored as hashes and
rotated from the Agent tab, day pass run tokens, and the v1 -> v2 migration."""

import hashlib
import sqlite3
import time

from app import store as store_mod
from app.store import Store

from .conftest import WORKER_KEY, mcp_call, register, sign_in

W = {"x-hallway-worker": WORKER_KEY}
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731


def dump(hallway) -> str:
    return "\n".join(hallway.store.db.iterdump())


# ------------------------------------------------------------------ sessions
async def test_each_sign_in_is_a_new_hashed_session(make_client, hallway):
    a, b = make_client(), make_client()
    me = await register(a, "Asha Rao", email="asha@example.com")
    await sign_in(b, "asha@example.com")
    ca, cb = a.cookies.get("hw_session"), b.cookies.get("hw_session")
    assert ca and cb and ca != cb
    assert (await a.get("/api/me")).status_code == 200 and (await b.get("/api/me")).status_code == 200

    rows = hallway.store.all("SELECT * FROM sessions")
    assert {r["id"] for r in rows} == {sha(ca), sha(cb)}
    assert all(r["attendee_id"] == hallway.store.attendee(handle="asha")["id"] for r in rows)
    assert all(r["expires_at"] - r["created_at"] == 7 * 86400 for r in rows)
    text = dump(hallway)
    assert ca not in text and cb not in text and me["connect"]["token"] not in text


async def test_logout_ends_only_this_session(make_client):
    a, b = make_client(), make_client()
    await register(a, "Asha Rao", email="asha@example.com")
    await sign_in(b, "asha@example.com")
    await a.post("/api/auth/logout")
    assert (await a.get("/api/me")).status_code == 401
    assert (await b.get("/api/me")).status_code == 200


async def test_sign_out_everywhere(make_client, hallway):
    a, b, other = make_client(), make_client(), make_client()
    await register(a, "Asha Rao", email="asha@example.com")
    await sign_in(b, "asha@example.com")
    await register(other, "Bo Chen")
    stolen = b.cookies.get("hw_session")
    r = await a.post("/api/auth/logout-all")
    assert r.json() == {"ok": True, "sessions": 2}
    assert "hw_session=" in r.headers["set-cookie"]
    assert (await a.get("/api/me")).status_code == 401
    b.cookies.set("hw_session", stolen)
    assert (await b.get("/api/me")).status_code == 401
    assert (await other.get("/api/me")).status_code == 200  # someone else's sessions stay
    assert (await a.post("/api/auth/logout-all")).status_code == 401


async def test_expired_session_is_refused_and_dropped(client, hallway):
    await register(client, "Asha Rao")
    sid = sha(client.cookies.get("hw_session"))
    hallway.store.db.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (time.time() - 1, sid))
    assert (await client.get("/api/me")).status_code == 401
    assert hallway.store.one("SELECT 1 FROM sessions WHERE id = ?", sid) is None


async def test_session_slides_on_use_but_not_every_request(client, hallway):
    await register(client, "Asha Rao")
    sid = sha(client.cookies.get("hw_session"))
    before = hallway.store.one("SELECT * FROM sessions WHERE id = ?", sid)
    await client.get("/api/me")
    assert hallway.store.one("SELECT * FROM sessions WHERE id = ?", sid) == before  # used just now: no write

    old = time.time() - 3 * 86400
    hallway.store.db.execute(
        "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?", (old, old + 7 * 86400, sid)
    )
    r = await client.get("/api/me")
    s = hallway.store.one("SELECT * FROM sessions WHERE id = ?", sid)
    assert s["last_seen_at"] > old and s["expires_at"] > time.time() + 7 * 86400 - 60
    # The cookie slides along with it (never shorter-lived than the 7 days it always had).
    assert "max-age=604" in r.headers["set-cookie"].lower()


async def test_session_has_an_absolute_cap(client, hallway):
    await register(client, "Asha Rao")
    sid = sha(client.cookies.get("hw_session"))
    created = time.time() - 30 * 86400 + 3600  # signed in almost 30 days ago, used ever since
    hallway.store.db.execute(
        "UPDATE sessions SET created_at = ?, last_seen_at = ? WHERE id = ?", (created, created, sid)
    )
    r = await client.get("/api/me")
    s = hallway.store.one("SELECT * FROM sessions WHERE id = ?", sid)
    assert s["expires_at"] == created + 30 * 86400
    assert int(r.headers["set-cookie"].lower().split("max-age=")[1].split(";")[0]) <= 3600
    hallway.store.db.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (time.time() - 1, sid))
    assert (await client.get("/api/me")).status_code == 401


async def test_onboarding_token_is_hashed_and_retired(client, hallway):
    await sign_in(client, "asha@example.com")
    pre = client.cookies.get("hw_session")
    assert hallway.store.kv_get(f"pre:{sha(pre)}") == "asha@example.com"
    assert hallway.store.kv_get(f"pre:{pre}") is None
    await client.post("/api/register", json={"name": "Asha Rao"})
    assert client.cookies.get("hw_session") != pre
    assert hallway.store.kv_get(f"pre:{sha(pre)}") is None
    client.cookies.set("hw_session", pre)
    assert (await client.get("/api/me")).status_code == 401


# ------------------------------------------------------------------ MCP keys
async def test_mcp_key_is_stored_hashed_and_works(signup, client, hallway):
    _, me = await signup("Asha Rao")
    tok = me["connect"]["token"]
    a = hallway.store.attendee(handle="asha")
    assert "mcp_token" not in a and a["mcp_token_hash"] == sha(tok) and a["mcp_token_hint"] == tok[:7] + "…"
    assert (await mcp_call(client, tok, "whoami"))["your_human"]["handle"] == "asha"
    wrong = tok[:-1] + ("y" if tok.endswith("x") else "x")  # always differs from tok
    assert (await mcp_call(client, wrong, "whoami")) == {"error": "Unknown Hallway token."}


async def test_rotate_agent_key(signup, client, hallway):
    pc, me = await signup("Asha Rao")
    old = me["connect"]["token"]
    r = await pc.post("/api/me/agent-key")
    assert r.status_code == 200
    new = r.json()["connect"]["token"]
    assert new.startswith("hw_") and new != old and new in r.json()["connect"]["claude_code"]
    assert (await mcp_call(client, old, "whoami")) == {"error": "Unknown Hallway token."}
    assert (await mcp_call(client, new, "whoami"))["your_human"]["handle"] == "asha"
    after = (await pc.get("/api/me")).json()["connect"]
    assert after["token"] is None and after["token_hint"] == new[:7] + "…" and new not in str(after)
    assert (await client.post("/api/me/agent-key")).status_code == 401


# ------------------------------------------------------------------ day pass
async def test_day_pass_worker_runs_with_its_own_token(signup, client, hallway):
    pc, _me = await signup("Sana Qureshi", mode="daypass")
    [due] = (await client.get("/internal/daypass/due", headers=W)).json()
    run_tok = due["token"]
    assert run_tok.startswith("hwp_") and run_tok not in dump(hallway)
    assert (await mcp_call(client, run_tok, "whoami"))["your_human"]["handle"] == "sana"
    # Rotating the attendee's own key doesn't stop the worker's run.
    await pc.post("/api/me/agent-key")
    assert (await mcp_call(client, run_tok, "whoami"))["your_human"]["handle"] == "sana"
    r = await client.post(
        f"/internal/daypass/{due['attendee_id']}/runs",
        headers=W,
        json={"engine": "test", "cost_usd": 0.01, "summary": "ok"},
    )
    assert r.status_code == 200
    # The run reported: its token is done.
    assert (await mcp_call(client, run_tok, "whoami")) == {"error": "Unknown Hallway token."}


async def test_expired_worker_token_is_refused(signup, client, hallway):
    await signup("Sana Qureshi", mode="daypass")
    [due] = (await client.get("/internal/daypass/due", headers=W)).json()
    hallway.store.db.execute("UPDATE worker_tokens SET expires_at = ?", (time.time() - 1,))
    assert (await mcp_call(client, due["token"], "whoami")) == {"error": "Unknown Hallway token."}


# ----------------------------------------------------------------- migration
V1_ATTENDEES = """
CREATE TABLE attendees (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, handle TEXT UNIQUE NOT NULL,
  agent_name TEXT UNIQUE NOT NULL, mode TEXT NOT NULL DEFAULT 'connect', profile TEXT NOT NULL DEFAULT '{}',
  contact TEXT NOT NULL DEFAULT '{}', autonomy TEXT NOT NULL DEFAULT '{}',
  mcp_token TEXT UNIQUE NOT NULL, session_token TEXT UNIQUE NOT NULL,
  mm_user_id TEXT, mm_password TEXT, mm_token TEXT, agent_mm_user_id TEXT, switch_agent_id TEXT,
  switch_agent_key TEXT, cursors TEXT NOT NULL DEFAULT '{}', privacy TEXT NOT NULL DEFAULT '{}',
  agent_seen_at REAL, is_persona INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
)"""


def downgrade_to_v1(db: sqlite3.Connection, plain: dict[str, tuple[str, str]], pre: dict[str, str]) -> None:
    """Rewrite a DB the way the pre-#8 code left it: plain tokens on attendees,
    no sessions table, plain `pre:<token>` keys, user_version 0."""
    cols = (
        "id, name, email, handle, agent_name, mode, profile, contact, autonomy, mm_user_id, mm_password, mm_token, "
        "agent_mm_user_id, switch_agent_id, switch_agent_key, cursors, privacy, agent_seen_at, is_persona, created_at"
    )
    db.execute("ALTER TABLE attendees RENAME TO attendees_new")
    db.execute(V1_ATTENDEES)
    db.execute(
        f"INSERT INTO attendees ({cols}, mcp_token, session_token) "
        f"SELECT {cols}, 'x' || id, 'y' || id FROM attendees_new"
    )
    db.execute("DROP TABLE attendees_new")
    for aid, (session, mcp) in plain.items():
        db.execute("UPDATE attendees SET session_token = ?, mcp_token = ? WHERE id = ?", (session, mcp, aid))
    db.execute("DROP TABLE sessions")
    db.execute("DROP TABLE worker_tokens")
    db.execute("DELETE FROM kv WHERE key LIKE 'pre:%'")
    for token, email in pre.items():
        db.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (f"pre:{token}", f'"{email}"'))
    db.execute("PRAGMA user_version = 0")


async def test_migration_from_plain_tokens(make_client, hallway, tmp_path):
    a, b, onboarding = make_client(), make_client(), make_client()
    asha = await register(a, "Asha Rao")
    await register(b, "Bo Chen")
    await sign_in(onboarding, "cy@example.com")
    aid = hallway.store.attendee(handle="asha")["id"]
    plain = {aid: ("legacy-session-asha", "hw_legacy_asha_key")}
    downgrade_to_v1(hallway.store.db, plain, {"legacy-pre": "cy@example.com"})
    path = tmp_path / "hallway.db"
    hallway.store.db.close()
    hallway.store = Store(str(path))

    db = hallway.store.db
    assert db.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    cols = {r[1] for r in db.execute("PRAGMA table_info(attendees)")}
    assert {"mcp_token_hash", "mcp_token_hint"} <= cols and not {"mcp_token", "session_token"} & cols
    assert len(hallway.store.attendees()) == 2
    text = dump(hallway)
    assert "legacy-session-asha" not in text and "hw_legacy_asha_key" not in text and "legacy-pre" not in text

    # Existing people keep working: the old cookie, the old MCP key, a half-done onboarding.
    a.cookies.set("hw_session", "legacy-session-asha")
    me = (await a.get("/api/me")).json()
    assert me["handle"] == "asha" and me["connect"]["token_hint"] == "hw_lega…" and me["profile"] == asha["profile"]
    assert (await mcp_call(a, "hw_legacy_asha_key", "whoami"))["your_human"]["handle"] == "asha"
    onboarding.cookies.set("hw_session", "legacy-pre")
    assert (await onboarding.get("/api/me")).json() == {"onboarding": True, "email": "cy@example.com"}

    # Running it again changes nothing.
    before = dump(hallway)
    hallway.store.db.close()
    hallway.store = Store(str(path))
    assert dump(hallway) == before
    assert (await mcp_call(a, "hw_legacy_asha_key", "whoami"))["your_human"]["handle"] == "asha"


def test_fresh_db_is_current(tmp_path):
    s = Store(str(tmp_path / "fresh.db"))
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    assert "session_token" not in {r[1] for r in s.db.execute("PRAGMA table_info(attendees)")}
    s.db.close()
