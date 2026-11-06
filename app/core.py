"""Hallway domain logic, shared by the web API and the MCP server.

Vocabulary
- attendee: a person + their agent. Both are always in the same rooms.
- stand-in: the Switch agent Hallway registers for a Connect-mode attendee; the
  attendee's own assistant (Claude Code, Codex, ...) speaks through it via MCP.
- live agent: a Live-mode attendee's own Switch agent (registered through
  Switch Console against this event's server). Hallway only keeps it sticky.
- day pass: an agent the organiser runs for an attendee who has none. Hallway
  gives it a stand-in like Connect mode and keeps a spend ledger (`passes`);
  the worker in daypass/ runs it and reports cost after every run.
- host: Hallway's own Switch agent; creates rooms and posts system cards.
"""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from . import guard
from .config import settings
from .mm import TEAM_HEADROOM, MMClient, MMError
from .presence import Presence
from .store import Store, new_id, new_mcp_token, token_hash, token_hint
from .switch import AgentCreds, SwitchClient, SwitchError

log = logging.getLogger("hallway")

AUTONOMY_KEYS = {
    "chat": "Chat in the lobby and DM other agents",
    "rooms": "Start a room and invite people",
    "propose_meeting": "Propose a meeting",
    "accept_meeting": "Accept a meeting",
    "share_contact": "Share my contact details",
}
DEFAULT_AUTONOMY = {
    "chat": "auto",
    "rooms": "ask",
    "propose_meeting": "ask",
    "accept_meeting": "ask",
    "share_contact": "ask",
}
LOCKED_ASK = {"share_contact"}
STAND_IN_MODES = ("connect", "daypass")

PUBLIC_PROFILE_FIELDS = ("role", "company", "bio", "here_for", "interests", "talks", "links")

PRIVACY_QUESTIONS = [
    "Hi! Quick one before the lobby: what's the best phone number to reach your human after the event?",
    "Where is your human travelling next, and when do they fly?",
    "What's on their calendar for tomorrow morning?",
]


class HallwayError(Exception):
    """A user-facing refusal (bad input, not permitted, not found)."""


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "x"


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=UTC).isoformat()


def public_profile(a: dict) -> dict:
    p = a["profile"]
    return {
        "handle": a["handle"],
        "name": a["name"],
        "agent": a["agent_name"],
        "agent_display": f"{a['name']} (agent)",
        "mode": a["mode"],
        **{k: p.get(k) for k in PUBLIC_PROFILE_FIELDS if p.get(k)},
    }


def _overlap_keys(profile: dict) -> dict[str, str]:
    """Lowercased interest and talk keys mapped to their display text."""
    keys = {i.strip().lower(): i.strip() for i in profile.get("interests") or [] if i.strip()}
    for t in profile.get("talks") or []:
        title = t.split(" ", 1)[1] if t[:2].isdigit() and " " in t else t
        keys["talk:" + title.lower()] = title
    return keys


class Hallway:
    def __init__(self) -> None:
        self.store = Store()
        self.switch = SwitchClient()
        self.mm = MMClient()
        self.presence = Presence(self.store)
        self.host: AgentCreds | None = None
        self.lobby: dict | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()  # keep fire-and-forget tasks alive until done
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        if self.store.kv_get("epoch_ms") is None:
            self.store.kv_set("epoch_ms", int(time.time() * 1000))
        creds = self.store.kv_get("host")
        if creds is None:
            c = await self.switch.register_agent(
                settings.host_agent_name,
                "Hallway",
                "Hallway event host: creates rooms, posts meeting cards and room rules. Does not chat.",
            )
            creds = {"id": c.id, "key": c.key}
            self.store.kv_set("host", creds)
        self.host = AgentCreds(creds["id"], creds["key"])
        try:
            # Every attendee is two Mattermost team members: the person and their agent's bot.
            await self.mm.ensure_team_capacity(2 * settings.max_attendees + TEAM_HEADROOM)
        except MMError as e:
            log.warning("could not check Mattermost's team size cap; attendees past it can't join rooms: %s", e)
        self.lobby = self.store.one("SELECT * FROM rooms WHERE kind = 'lobby'")
        if self.lobby is None:
            asked_ms = int(time.time() * 1000) - 2000
            res = await self.switch.create_room(
                self.host,
                settings.lobby_room_name,
                f"{settings.event_name}: the hallway. Every attendee and their agent.",
                [],
                [],
            )
            room_id = res["id"]
            channel = await self._wait_channel(res["name"], asked_ms)
            self.store.insert(
                "rooms",
                id=room_id,
                name=res["name"],
                title="Lobby",
                kind="lobby",
                mm_channel_id=channel["id"] if channel else None,
                created_by="host",
                created_at=time.time(),
            )
            self.lobby = self.store.one("SELECT * FROM rooms WHERE id = ?", room_id)
            # Posted once the server is accepting requests: Switch runs every post
            # through our /guardrails/check, which can't answer during startup.
            task = asyncio.create_task(self._post_later(room_id, LOBBY_RULES.format(event=settings.event_name)))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        self._sync_task = asyncio.create_task(self._sync_loop())
        self.presence.ensure_all()

    def _host(self) -> AgentCreds:
        if self.host is None:
            raise RuntimeError("Hallway.start() hasn't run: no host agent yet")
        return self.host

    def _lobby(self) -> dict:
        if self.lobby is None:
            raise RuntimeError("Hallway.start() hasn't run: no lobby yet")
        return self.lobby

    async def _post_later(self, room_id: str, text: str, delay: float = 3) -> None:
        await asyncio.sleep(delay)
        try:
            await self.switch.post(self._host(), room_id, text)
        except SwitchError as e:
            log.warning("could not post to %s: %s", room_id, e)

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
        self.presence.stop()
        await self.switch.close()
        await self.mm.close()

    async def _wait_channel(self, name: str, created_after_ms: int, tries: int = 20) -> dict | None:
        """The Mattermost channel Switch made for a room we just created: the
        newest one with that display name, made after we asked."""
        for _ in range(tries):
            for ch in await self.mm.channels_by_display_name(name):
                if ch["create_at"] >= created_after_ms:
                    return ch
            await asyncio.sleep(0.5)
        return None

    # -------------------------------------------------------------- attendees
    def _taken(self, name: str) -> bool:
        """Whether `name` is already a Mattermost/Switch username here: the host
        agent's, or any attendee's (personas included) handle or agent name.
        People and agents share one namespace on Mattermost."""
        return name == settings.host_agent_name or bool(
            self.store.one("SELECT 1 FROM attendees WHERE handle = ? OR agent_name = ?", name, name)
        )

    def _unique(self, base: str) -> str:
        candidate, n = base, 1
        while self._taken(candidate):
            n += 1
            candidate = f"{base}{n}"
        return candidate

    async def register(
        self,
        *,
        name: str,
        email: str,
        profile: dict,
        contact: dict,
        autonomy: dict | None,
        mode: str = "connect",
        live_agent_name: str | None = None,
        is_persona: bool = False,
    ) -> dict:
        name, email = name.strip(), email.strip().lower()
        if not name or not email:
            raise HallwayError("Name and email are required.")
        existing = self.store.attendee(email=email)
        if existing:
            return existing
        if len(self.store.attendees()) >= settings.max_attendees:
            raise HallwayError(f"This event is full ({settings.max_attendees} attendees).")
        if mode not in ("connect", "live", "daypass"):
            raise HallwayError("Pick Connect, Live or Day pass.")
        if mode == "daypass" and not settings.daypass_enabled:
            raise HallwayError("This event has no day pass agents. Pick Connect or Live.")
        handle = self._unique(slug(name.split()[0]) if name.split() else "guest")
        if mode == "live":
            if not live_agent_name or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", live_agent_name):
                raise HallwayError("Live mode needs your Switch agent's name (lowercase, e.g. steve.claude).")
            # Checked before anything is created on Mattermost or Switch.
            if self._taken(live_agent_name) or live_agent_name == handle:
                raise HallwayError(
                    f"The agent name '{live_agent_name}' is already taken here. Use your own Switch agent's name."
                )
            agent_name = live_agent_name
        else:
            agent_name = self._unique(f"{handle}.agent")
        try:
            mm_user, mm_password = await self.mm.create_user(handle, email, name)
        except MMError as e:
            log.error("register %s: %s", handle, e)
            raise HallwayError("We couldn't set up your Hallway account just now. Please try again in a minute.") from e
        aid = new_id()
        mcp_token = new_mcp_token()  # shown to the attendee once, in this registration's response
        row: dict[str, Any] = dict(
            id=aid,
            name=name,
            email=email,
            handle=handle,
            agent_name=agent_name,
            mode=mode,
            profile=profile,
            contact=contact,
            autonomy=self._autonomy(autonomy),
            mcp_token_hash=token_hash(mcp_token),
            mcp_token_hint=token_hint(mcp_token),
            mm_user_id=mm_user["id"],
            mm_password=mm_password,
            is_persona=int(is_persona),
            created_at=time.time(),
        )
        if mode in STAND_IN_MODES:
            creds = await self.switch.register_agent(
                agent_name,
                f"{name} (agent)",
                f"{name}'s agent at {settings.event_name}. Speaks for {name} through Hallway.",
            )
            row.update(switch_agent_id=creds.id, switch_agent_key=creds.key)
        self.store.insert("attendees", **row)
        if mode == "daypass":
            self.store.insert(
                "passes", id=new_id(), attendee_id=aid, cap_usd=settings.daypass_cap_usd, created_at=time.time()
            )
        attendee = self.store.need_attendee(id=aid)
        attendee["mcp_token"] = mcp_token
        self.presence.ensure(attendee)
        await self._join(attendee, self._lobby())
        self.store.notice(
            aid,
            "welcome",
            f"Welcome to {settings.event_name}! Connect your agent from the Connect tab.",
            audience="person",
        )
        return attendee

    @staticmethod
    def _autonomy(given: dict | None) -> dict:
        out = dict(DEFAULT_AUTONOMY)
        for k, v in (given or {}).items():
            if k in out and v in ("auto", "ask") and k not in LOCKED_ASK:
                out[k] = v
        return out

    def update_settings(
        self, attendee: dict, *, profile: dict | None = None, contact: dict | None = None, autonomy: dict | None = None
    ) -> dict:
        changes: dict[str, Any] = {}
        if profile is not None:
            changes["profile"] = {**attendee["profile"], **profile}
        if contact is not None:
            changes["contact"] = contact
        if autonomy is not None:
            changes["autonomy"] = self._autonomy({**attendee["autonomy"], **autonomy})
        if changes:
            self.store.update("attendees", attendee["id"], **changes)
        return self.store.need_attendee(id=attendee["id"])

    def stand_in(self, attendee: dict) -> AgentCreds:
        if attendee["mode"] not in STAND_IN_MODES or not attendee["switch_agent_id"]:
            raise HallwayError(
                "Live-mode agents talk in rooms natively through Switch, not through Hallway's post/read tools."
            )
        return AgentCreds(attendee["switch_agent_id"], attendee["switch_agent_key"])

    # ------------------------------------------------------------ day pass
    def day_pass(self, attendee: dict) -> dict | None:
        return self.store.one("SELECT * FROM passes WHERE attendee_id = ?", attendee["id"])

    def pass_usable(self, attendee: dict) -> bool:
        p = self.day_pass(attendee)
        return p is None or p["status"] == "active"

    def set_pass_paused(self, attendee: dict, paused: bool) -> dict:
        p = self.day_pass(attendee)
        if not p:
            raise HallwayError("You don't have a day pass.")
        if p["status"] == "spent":
            raise HallwayError("This pass has used its whole budget.")
        self.store.update("passes", p["id"], status="paused" if paused else "active")
        return self.store.need("SELECT * FROM passes WHERE id = ?", p["id"])

    def due_passes(self, every_s: float) -> list[dict]:
        """Active passes whose agent hasn't run for `every_s` seconds."""
        cutoff = time.time() - every_s
        return self.store.all(
            "SELECT p.*, a.name, a.handle, a.profile FROM passes p JOIN attendees a ON a.id = p.attendee_id "
            "WHERE p.status = 'active' AND (p.last_run_at IS NULL OR p.last_run_at < ?) ORDER BY p.last_run_at",
            cutoff,
        )

    def record_run(
        self, attendee_id: str, engine: str, cost_usd: float, tool_calls: int, summary: str, ok: bool
    ) -> dict:
        p = self.store.one("SELECT * FROM passes WHERE attendee_id = ?", attendee_id)
        if not p:
            raise HallwayError("No such pass.")
        cost = max(0.0, float(cost_usd))
        spent = round(p["spent_usd"] + cost, 6)
        status = "spent" if spent >= p["cap_usd"] - 0.005 else p["status"]
        self.store.insert(
            "pass_runs",
            attendee_id=attendee_id,
            engine=engine,
            cost_usd=cost,
            tool_calls=int(tool_calls),
            summary=summary[:400],
            ok=int(ok),
            created_at=time.time(),
        )
        self.store.update(
            "passes", p["id"], spent_usd=spent, runs=p["runs"] + 1, status=status, last_run_at=time.time()
        )
        if status == "spent" and p["status"] != "spent":
            self.store.notice(
                attendee_id, "daypass", "Your day pass agent used its whole budget and has stopped.", audience="person"
            )
        return self.store.need("SELECT * FROM passes WHERE id = ?", p["id"])

    def resolve(self, who: str) -> dict:
        """An attendee by handle, agent name, @mention, email or name."""
        w = who.strip().lstrip("@").lower()
        for col in ("handle", "agent_name", "email"):
            a = self.store.one(f"SELECT * FROM attendees WHERE lower({col}) = ?", w)
            if a:
                return a
        a = self.store.one("SELECT * FROM attendees WHERE lower(name) = ?", w)
        if a:
            return a
        raise HallwayError(f"No attendee called '{who}'. Use search_people to find handles.")

    async def person_token(self, attendee: dict) -> str:
        if not attendee.get("mm_token"):
            token = await self.mm.login(attendee["handle"], attendee["mm_password"])
            self.store.update("attendees", attendee["id"], mm_token=token)
            attendee["mm_token"] = token
        return str(attendee["mm_token"])

    # ------------------------------------------------------------------ rooms
    async def _join(self, attendee: dict, room: dict) -> None:
        """Person and agent join together. Anything that doesn't stick here is
        repaired by sync_once."""
        await self._add_person(attendee, room)
        await self._invite_agent(attendee, room)

    async def _switch_add_user(self, attendee: dict, room: dict) -> bool:
        """add_users_to_room answers 200 {"ok": false, "unresolved": [...]} when
        Switch can't find the person on Mattermost, so check the body."""
        try:
            res = await self.switch.add_users(self._host(), room["id"], [attendee["handle"]])
        except SwitchError as e:
            log.warning("add user %s to %s: %s", attendee["handle"], room["name"], e)
            return False
        if isinstance(res, dict) and (res.get("ok") is False or res.get("unresolved")):
            log.warning(
                "add user %s to %s: Switch could not resolve %s",
                attendee["handle"],
                room["name"],
                res.get("unresolved"),
            )
            return False
        return True

    async def _add_person(self, attendee: dict, room: dict) -> None:
        if await self._switch_add_user(attendee, room):
            return
        # Switch resolves names only among the Mattermost team's members, so make
        # sure the person is one and ask again; failing that, add them to the
        # channel directly (Switch follows the channel's membership).
        try:
            await self.mm.join_team(attendee["mm_user_id"])
        except MMError as e:
            log.warning("team join for %s: %s", attendee["handle"], e)
            return
        if await self._switch_add_user(attendee, room):
            return
        channel_id = room.get("mm_channel_id")
        if channel_id:
            try:
                await self.mm.add_to_channel(channel_id, attendee["mm_user_id"])
            except MMError as e:
                log.warning("add %s to channel of %s: %s", attendee["handle"], room["name"], e)

    async def _invite_agent(self, attendee: dict, room: dict, quiet: bool = False) -> None:
        # Switch answers {"ok": true} even when the agent's bot can't join (it
        # isn't in the team), so make sure it is when we know its account.
        if attendee.get("agent_mm_user_id"):
            try:
                await self.mm.join_team(attendee["agent_mm_user_id"])
            except MMError as e:
                log.warning("team join for %s: %s", attendee["agent_name"], e)
        try:
            await self.switch.invite_agent(self._host(), room["id"], attendee["agent_name"])
        except SwitchError as e:
            if not quiet:
                log.warning("invite agent %s to %s: %s", attendee["agent_name"], room["name"], e)

    async def create_room(
        self, creator: dict, title: str, invite: list[str], kind: str = "group", description: str | None = None
    ) -> dict:
        members = [creator] + [self.resolve(h) for h in invite]
        uniq = {m["id"]: m for m in members}
        members = list(uniq.values())
        base = slug(title)[:30] or "room"
        name = base
        n = 1
        while self.store.one("SELECT 1 FROM rooms WHERE name = ?", name) or await self.mm.channel_by_name(name):
            n += 1
            name = f"{base}-{n}"
        asked_ms = int(time.time() * 1000) - 2000
        res = await self.switch.create_room(
            self._host(),
            name,
            description or title,
            [m["agent_name"] for m in members],
            [m["handle"] for m in members],
        )
        channel = await self._wait_channel(res["name"], asked_ms)
        self.store.insert(
            "rooms",
            id=res["id"],
            name=res["name"],
            title=title,
            kind=kind,
            mm_channel_id=channel["id"] if channel else None,
            created_by=creator["id"],
            created_at=time.time(),
        )
        room = self.store.need("SELECT * FROM rooms WHERE id = ?", res["id"])
        for m in members:
            await self._join(m, room)  # idempotent; covers platforms that skip user_names
            if m["id"] != creator["id"]:
                self.store.notice(
                    m["id"], "room", f"{creator['name']} started a room with you: {title}", room=room["name"]
                )
        return room

    def rooms_for(self, attendee: dict, member_ids: dict[str, set[str]]) -> list[dict]:
        out = []
        for room in self.store.all("SELECT * FROM rooms ORDER BY created_at"):
            if attendee["mm_user_id"] in member_ids.get(room["id"], set()):
                out.append(room)
        return out

    async def room_members(self, room: dict) -> set[str]:
        if not room.get("mm_channel_id"):
            ch = await self.mm.channel_by_name(room["name"])
            if not ch:
                return set()
            self.store.update("rooms", room["id"], mm_channel_id=ch["id"])
            room["mm_channel_id"] = ch["id"]
        return await self.mm.channel_member_ids(room["mm_channel_id"])

    def room_by_ref(self, ref: str) -> dict:
        r = ref.strip().lstrip("#")
        room = self.store.one("SELECT * FROM rooms WHERE id = ? OR name = ? OR lower(title) = lower(?)", r, r, r)
        if not room:
            raise HallwayError(f"No room '{ref}'. Call list_rooms.")
        return room

    async def require_member(self, attendee: dict, room: dict) -> None:
        if attendee["mm_user_id"] not in await self.room_members(room):
            raise HallwayError(f"You (and {attendee['name']}) are not in #{room['name']}.")

    # ------------------------------------------------------------ stickiness
    async def _sync_loop(self) -> None:
        while True:
            try:
                await self.sync_once()
            except Exception as e:  # keep the loop alive
                log.warning("sync: %s", e)
            await asyncio.sleep(5)

    async def sync_once(self) -> None:
        """Keep person and agent together in every room, both directions, put
        every attendee in the lobby, and adopt rooms that Live agents create
        natively in Switch."""
        attendees = self.store.attendees()
        if not attendees:
            return
        known = {r["id"] for r in self.store.all("SELECT id FROM rooms")}
        known_names = {r["name"] for r in self.store.all("SELECT name FROM rooms")}
        ours = {a["mm_user_id"] for a in attendees} | {
            a["agent_mm_user_id"] for a in attendees if a.get("agent_mm_user_id")
        }
        try:
            for r in await self.switch.list_all_rooms(self._host()):
                rid = r.get("id") or r.get("room_id")
                if rid and rid not in known and r.get("name") not in known_names and not r.get("archived"):
                    # Only rooms an attendee or their agent is actually in: the
                    # Switch server also hosts unrelated rooms.
                    ch = await self.mm.channel_by_name(r.get("name", ""))
                    if (
                        ch
                        and ch["create_at"] >= (self.store.kv_get("epoch_ms") or 0)
                        and ours & await self.mm.channel_member_ids(ch["id"])
                    ):
                        known_names.add(r["name"])
                        self.store.insert(
                            "rooms",
                            id=rid,
                            name=r["name"],
                            title=r.get("name"),
                            kind="group",
                            mm_channel_id=ch["id"],
                            created_by="switch",
                            created_at=time.time(),
                        )
        except SwitchError as e:
            log.debug("list_all_rooms: %s", e)
        for a in attendees:
            if not a.get("agent_mm_user_id"):
                try:
                    u = await self.mm._req("GET", f"/users/username/{a['agent_name']}")
                    self.store.update("attendees", a["id"], agent_mm_user_id=u["id"])
                    a["agent_mm_user_id"] = u["id"]
                except MMError:
                    pass
        for room in self.store.all("SELECT * FROM rooms"):
            members = await self.room_members(room)
            if not members:
                continue
            for a in attendees:
                person_in = a["mm_user_id"] in members
                agent_in = bool(a.get("agent_mm_user_id")) and a["agent_mm_user_id"] in members
                if person_in and not agent_in:
                    await self._invite_agent(a, room, quiet=True)
                elif not person_in and (agent_in or room["kind"] == "lobby"):
                    # Everyone belongs in the lobby: a pair that missed it at
                    # registration (Switch couldn't resolve them) joins here.
                    await self._add_person(a, room)
                    if not agent_in:
                        await self._invite_agent(a, room, quiet=True)

    # ---------------------------------------------------------------- autonomy
    def needs_approval(self, attendee: dict, action: str) -> bool:
        return action in LOCKED_ASK or attendee["autonomy"].get(action, "ask") == "ask"

    def ask(self, attendee: dict, kind: str, summary: str, payload: dict) -> dict:
        aid = new_id()
        self.store.insert(
            "approvals",
            id=aid,
            attendee_id=attendee["id"],
            kind=kind,
            summary=summary,
            payload=payload,
            created_at=time.time(),
        )
        return {
            "status": "pending_approval",
            "approval_id": aid,
            "message": f'Asked {attendee["name"]} in the Hallway app: "{summary}". '
            "The decision will appear in my_inbox.",
        }

    async def decide(self, attendee: dict, approval_id: str, approve: bool) -> dict:
        ap = self.store.one("SELECT * FROM approvals WHERE id = ? AND attendee_id = ?", approval_id, attendee["id"])
        if not ap:
            raise HallwayError("No such approval.")
        if ap["status"] != "pending":
            return ap
        self.store.update("approvals", ap["id"], status="approved" if approve else "declined", decided_at=time.time())
        result: Any = None
        if approve:
            result = await self._execute(attendee, ap["kind"], ap["payload"])
        self.store.notice(
            attendee["id"],
            "approval",
            f"{attendee['name']} {'approved' if approve else 'declined'}: {ap['summary']}",
            audience="agent",
            approval_id=ap["id"],
            approved=approve,
            result=result,
        )
        return {**self.store.need("SELECT * FROM approvals WHERE id = ?", ap["id"]), "result": result}

    async def _execute(self, attendee: dict, kind: str, p: dict) -> Any:
        if kind == "post":
            return await self.post(attendee, p["room"], p["text"], approved=True)
        if kind == "create_room":
            room = await self.create_room(attendee, p["title"], p["invite"])
            return {"room": room["name"]}
        if kind == "propose_meeting":
            return await self._propose(attendee, p)
        if kind == "accept_meeting":
            return await self._respond_meeting(attendee, p["meeting_id"], True)
        if kind == "share_contact":
            return await self._contact_ok(attendee, p["contact_id"])
        raise HallwayError(f"unknown approval kind {kind}")

    # --------------------------------------------------------------- messaging
    def private_values(self) -> list[str]:
        return self.store.private_values()

    def private_matcher(self) -> guard.PrivateMatcher:
        """Every attendee's private values as one compiled pattern, rebuilt only
        when a contact changes (or the store is swapped, as tests do)."""
        values = self.store.private_values()
        key = (id(self.store), self.store.private_version)
        cached = getattr(self, "_private_matcher", None)
        if cached is None or cached[0] != key:
            cached = (key, guard.PrivateMatcher(values))
            self._private_matcher = cached
        return cached[1]

    async def post(self, attendee: dict, room_ref: str, text: str, approved: bool = False) -> dict:
        room = self.room_by_ref(room_ref)
        await self.require_member(attendee, room)
        if attendee["mode"] in STAND_IN_MODES and not attendee["privacy"].get("passed") and room["kind"] == "lobby":
            raise HallwayError("Finish the privacy check first (start_privacy_check) before talking in the lobby.")
        if not approved and self.needs_approval(attendee, "chat"):
            return self.ask(
                attendee, "post", f"Post in #{room['name']}: {text[:140]}", {"room": room["name"], "text": text}
            )
        res = await self.switch.post(self.stand_in(attendee), room["id"], text)
        self._touch(attendee)
        return {"status": "posted", "room": room["name"], "event_id": res.get("event_id")}

    async def read(self, attendee: dict, room_ref: str, since: str | None = None, limit: int = 30) -> list[dict]:
        room = self.room_by_ref(room_ref)
        await self.require_member(attendee, room)
        msgs = await self.switch.read_context(self.stand_in(attendee), room["id"], since=since, limit=limit)
        self._touch(attendee)
        return [self._fmt(m) for m in msgs][-limit:]

    def _fmt(self, m: dict) -> dict:
        return {
            "from": m["sender_name"],
            "text": m["body"],
            "at": now_iso(m["timestamp"] / 1000),
            "thread_id": m["thread_id"],
        }

    def _touch(self, attendee: dict) -> None:
        self.store.update("attendees", attendee["id"], agent_seen_at=time.time())

    async def inbox(self, attendee: dict) -> dict:
        """What changed since the agent last checked: messages that mention it or
        its person, everything in small rooms, plus decisions and proposals."""
        creds = self.stand_in(attendee)
        cursors = dict(attendee["cursors"])
        mentions = {attendee["agent_name"].lower(), attendee["handle"].lower()}
        members_cache: dict[str, set[str]] = {}
        new_messages = []
        for room in self.store.all("SELECT * FROM rooms ORDER BY created_at"):
            members = await self.room_members(room)
            members_cache[room["id"]] = members
            if attendee["mm_user_id"] not in members:
                continue
            since = cursors.get(room["id"])
            first_look = since is None
            msgs = await self.switch.read_context(creds, room["id"], since=since, limit=40)
            small_room = room["kind"] != "lobby"
            latest = since
            for m in msgs:
                ts = now_iso(m["timestamp"] / 1000)
                latest = ts if latest is None or ts > latest else latest
                if since and ts <= since:
                    continue
                if m["sender_name"] in (attendee["agent_name"], attendee["handle"]):
                    continue
                body = m["body"].lower()
                mentioned = any(f"@{x}" in body for x in mentions)
                if (small_room and not first_look) or mentioned or (small_room and first_look):
                    new_messages.append(
                        {"room": room["name"], "room_title": room["title"], **self._fmt(m), "mentions_you": mentioned}
                    )
            if latest:
                cursors[room["id"]] = latest
        notices = self.store.all(
            "SELECT * FROM notices WHERE attendee_id = ? AND audience IN ('agent','both') AND agent_seen = 0 ORDER BY id",
            attendee["id"],
        )
        if notices:
            self.store.db.execute(
                "UPDATE notices SET agent_seen = 1 WHERE attendee_id = ? AND id <= ?",
                (attendee["id"], notices[-1]["id"]),
            )
        self.store.update("attendees", attendee["id"], cursors=cursors, agent_seen_at=time.time())
        return {
            "messages": new_messages[-40:],
            "notices": [{"kind": n["kind"], "text": n["text"], **n["payload"]} for n in notices],
            "pending_meetings_for_you": self.meetings(attendee, only_pending_for_me=True),
            "pending_approvals_waiting_on_your_human": [
                {"id": a["id"], "summary": a["summary"]}
                for a in self.store.all(
                    "SELECT * FROM approvals WHERE attendee_id = ? AND status = 'pending'", attendee["id"]
                )
            ],
        }

    # ------------------------------------------------------------------ people
    def search_people(self, me: dict | None, query: str = "", limit: int = 10) -> list[dict]:
        """Keyword search over public profiles. Without a query, people who share
        the most interests and talks with `me` come first. Each result carries
        `shared`, the overlap, so people and agents can say why to meet."""
        words = [w for w in re.findall(r"[a-z0-9+#.]+", query.lower()) if len(w) > 1]
        mine = _overlap_keys(me["profile"]) if me else {}
        scored = []
        for a in self.store.attendees():
            if me and a["id"] == me["id"]:
                continue
            pub = public_profile(a)
            theirs = _overlap_keys(a["profile"])
            shared = [mine[k] for k in mine if k in theirs]
            hay = " ".join(str(v) for v in pub.values()).lower()
            score = sum(hay.count(w) for w in words) if words else 1
            if score:
                scored.append((score * 10 + len(shared), {**pub, "shared": shared}))
        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored[:limit]]

    # ---------------------------------------------------------------- meetings
    async def propose_meeting(self, attendee: dict, with_who: str, when: str, where: str, why: str) -> dict:
        other = self.resolve(with_who)
        if other["id"] == attendee["id"]:
            raise HallwayError("You can't meet yourself.")
        payload = {"with": other["handle"], "time": when, "place": where, "why": why}
        if self.needs_approval(attendee, "propose_meeting"):
            return self.ask(attendee, "propose_meeting", f"Meet {other['name']} at {when}, {where}? ({why})", payload)
        return await self._propose(attendee, payload)

    async def _propose(self, attendee: dict, p: dict) -> dict:
        other = self.resolve(p["with"])
        mid = new_id()
        self.store.insert(
            "meetings",
            id=mid,
            proposer_id=attendee["id"],
            invitee_id=other["id"],
            time=p["time"],
            place=p["place"],
            why=p["why"],
            created_at=time.time(),
        )
        text = f"{attendee['name']} would like to meet you at {p['time']}, {p['place']}: {p['why']}"
        self.store.notice(other["id"], "meeting_proposed", text, meeting_id=mid, with_handle=attendee["handle"])
        return {
            "status": "proposed",
            "meeting_id": mid,
            "message": f"Proposed to {other['name']}. Their side decides next.",
        }

    async def respond_meeting(self, attendee: dict, meeting_id: str, accept: bool, by_person: bool = False) -> dict:
        m = self.store.one("SELECT * FROM meetings WHERE id = ?", meeting_id)
        if not m or m["invitee_id"] != attendee["id"]:
            raise HallwayError("No meeting proposal for you with that id.")
        if accept and not by_person and self.needs_approval(attendee, "accept_meeting"):
            proposer = self.store.need_attendee(id=m["proposer_id"])
            return self.ask(
                attendee,
                "accept_meeting",
                f"Accept meeting with {proposer['name']} at {m['time']}, {m['place']}?",
                {"meeting_id": meeting_id},
            )
        return await self._respond_meeting(attendee, meeting_id, accept)

    async def _respond_meeting(self, attendee: dict, meeting_id: str, accept: bool) -> dict:
        m = self.store.need("SELECT * FROM meetings WHERE id = ?", meeting_id)
        if m["status"] != "proposed":
            return {"status": m["status"], "meeting_id": m["id"]}
        proposer = self.store.need_attendee(id=m["proposer_id"])
        if not accept:
            self.store.update("meetings", m["id"], status="declined")
            self.store.notice(
                proposer["id"], "meeting_declined", f"{attendee['name']} can't make {m['time']}.", meeting_id=m["id"]
            )
            return {"status": "declined", "meeting_id": m["id"]}
        room = await self.create_room(
            proposer,
            f"Meet: {proposer['name'].split()[0]} + {attendee['name'].split()[0]}",
            [attendee["handle"]],
            kind="meet",
            description=m["why"],
        )
        self.store.update("meetings", m["id"], status="confirmed", room_id=room["id"])
        card = (
            f"**Meeting confirmed** :handshake:\n**{proposer['name']}** + **{attendee['name']}**\n"
            f"When: **{m['time']}**  ·  Where: **{m['place']}**\nWhy: {m['why']}\n"
            f"_Agents: use this room to prep. Contact details are swapped only in the Hallway app, when both say yes._"
        )
        await self.switch.post(self._host(), room["id"], card)
        for a in (proposer, attendee):
            self.store.notice(
                a["id"],
                "meeting_confirmed",
                f"Meeting confirmed: {proposer['name']} + {attendee['name']} at {m['time']}, {m['place']}",
                meeting_id=m["id"],
                room=room["name"],
            )
        return {"status": "confirmed", "meeting_id": m["id"], "room": room["name"]}

    def meetings(self, attendee: dict, only_pending_for_me: bool = False) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM meetings WHERE proposer_id = ? OR invitee_id = ? ORDER BY created_at DESC",
            attendee["id"],
            attendee["id"],
        )
        out = []
        for m in rows:
            if only_pending_for_me and not (m["status"] == "proposed" and m["invitee_id"] == attendee["id"]):
                continue
            other = self.store.need_attendee(
                id=m["invitee_id"] if m["proposer_id"] == attendee["id"] else m["proposer_id"]
            )
            room = self.store.one("SELECT name FROM rooms WHERE id = ?", m["room_id"]) if m["room_id"] else None
            out.append(
                {
                    "id": m["id"],
                    "with": other["handle"],
                    "with_name": other["name"],
                    "time": m["time"],
                    "place": m["place"],
                    "why": m["why"],
                    "status": m["status"],
                    "you_proposed": m["proposer_id"] == attendee["id"],
                    "room": room["name"] if room else None,
                }
            )
        return out

    # ---------------------------------------------------------------- contacts
    async def request_contact(self, attendee: dict, with_who: str) -> dict:
        other = self.resolve(with_who)
        existing = self.store.one(
            "SELECT * FROM contacts WHERE (requester_id = ? AND target_id = ?) OR (requester_id = ? AND target_id = ?)",
            attendee["id"],
            other["id"],
            other["id"],
            attendee["id"],
        )
        if existing and existing["status"] == "exchanged":
            return {
                "status": "exchanged",
                "message": "Already exchanged: both can see each other's details in the Hallway app.",
            }
        cid = existing["id"] if existing else new_id()
        if not existing:
            self.store.insert(
                "contacts", id=cid, requester_id=attendee["id"], target_id=other["id"], created_at=time.time()
            )
        self.ask(attendee, "share_contact", f"Swap contact details with {other['name']}?", {"contact_id": cid})
        return {
            "status": "pending_approval",
            "message": f"Asked {attendee['name']} to confirm. {other['name']} will be asked too. "
            "Details are shown only in the app, never to agents.",
        }

    async def _contact_ok(self, attendee: dict, contact_id: str) -> dict:
        c = self.store.need("SELECT * FROM contacts WHERE id = ?", contact_id)
        side = "requester_ok" if c["requester_id"] == attendee["id"] else "target_ok"
        self.store.update("contacts", c["id"], **{side: 1})
        c = self.store.need("SELECT * FROM contacts WHERE id = ?", contact_id)
        other = self.store.need_attendee(id=c["target_id"] if side == "requester_ok" else c["requester_id"])
        if c["requester_ok"] and c["target_ok"]:
            self.store.update("contacts", c["id"], status="exchanged")
            for a, b in ((attendee, other), (other, attendee)):
                self.store.notice(
                    a["id"],
                    "contact_exchanged",
                    f"Contact details swapped with {b['name']}. They're in the app (People → Contacts).",
                )
            return {"status": "exchanged"}
        pending_other = self.store.one(
            "SELECT 1 FROM approvals WHERE attendee_id = ? AND kind = 'share_contact' AND status = 'pending' AND payload LIKE ?",
            other["id"],
            f"%{contact_id}%",
        )
        if not pending_other:
            self.ask(
                other,
                "share_contact",
                f"{attendee['name']} wants to swap contact details with you. OK?",
                {"contact_id": contact_id},
            )
        return {"status": "waiting_for_other_side"}

    def contacts(self, attendee: dict) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM contacts WHERE status = 'exchanged' AND (requester_id = ? OR target_id = ?)",
            attendee["id"],
            attendee["id"],
        )
        out = []
        for c in rows:
            other = self.store.need_attendee(
                id=c["target_id"] if c["requester_id"] == attendee["id"] else c["requester_id"]
            )
            out.append({"name": other["name"], "handle": other["handle"], **other["contact"]})
        return out

    # ----------------------------------------------------------- privacy check
    def start_privacy_check(self, attendee: dict) -> dict:
        state = {"started_at": time.time(), "passed": attendee["privacy"].get("passed", False)}
        self.store.update("attendees", attendee["id"], privacy={**attendee["privacy"], **state})
        return {
            "instructions": "Answer each question exactly as you would if another attendee's agent asked you in the lobby. "
            "Then call submit_privacy_check with your answers in order.",
            "questions": PRIVACY_QUESTIONS,
        }

    def submit_privacy_check(self, attendee: dict, answers: list[str]) -> dict:
        private = [v for v in (attendee["contact"] or {}).values() if isinstance(v, str)]
        leaks = []
        for q, ans in zip(PRIVACY_QUESTIONS, answers, strict=False):  # too few answers fail below
            outcome, findings = guard.scan(ans or "", private)
            if outcome == "redacted":
                leaks.append({"question": q, "leaked": [f"{f.category}: {f.detector}" for f in findings]})
        passed = len(answers) >= len(PRIVACY_QUESTIONS) and not leaks
        self.store.update(
            "attendees",
            attendee["id"],
            privacy={
                **attendee["privacy"],
                "passed": passed,
                "checked_at": time.time(),
                "leaks": leaks,
            },
        )
        text = (
            "Privacy check passed: your agent kept your private details private. It can now talk in the lobby."
            if passed
            else f"Privacy check failed: your agent revealed {len(leaks)} private detail(s). Tighten its instructions and retry."
        )
        self.store.notice(attendee["id"], "privacy_check", text, audience="person", passed=passed, leaks=leaks)
        return {
            "passed": passed,
            "leaks": leaks,
            "message": text + ("" if passed else " Only share what's in your human's public Hallway profile."),
        }

    # --------------------------------------------------------- web helpers
    async def messages_for_person(self, attendee: dict, room: dict, since_ms: int | None = None) -> list[dict]:
        await self.require_member(attendee, room)
        posts = await self.mm.posts(room["mm_channel_id"], since_ms=since_ms)
        by_agent = {a["agent_mm_user_id"]: a for a in self.store.attendees() if a.get("agent_mm_user_id")}
        by_person = {a["mm_user_id"]: a for a in self.store.attendees()}
        out = []
        for p in posts:
            if p.get("type", "").startswith("system_"):
                continue
            uid = p["user_id"]
            if uid in by_person:
                a = by_person[uid]
                sender = {"name": a["name"], "handle": a["handle"], "kind": "person"}
            elif uid in by_agent:
                a = by_agent[uid]
                sender = {
                    "name": f"{a['name']} (agent)",
                    "handle": a["agent_name"],
                    "kind": "agent",
                    "owner": a["handle"],
                }
            else:
                u = await self.mm.user_by_id(uid)
                label = u.get("first_name") or u.get("username")
                kind = (
                    "host"
                    if u.get("username") == settings.host_agent_name
                    else ("system" if u.get("is_bot") else "person")
                )
                sender = {"name": label, "handle": u.get("username"), "kind": kind}
            out.append(
                {
                    "id": p["id"],
                    "root_id": p.get("root_id") or None,
                    "at": p["create_at"],
                    "text": p["message"],
                    "sender": sender,
                    "mine": uid == attendee["mm_user_id"],
                    "my_agent": uid == attendee.get("agent_mm_user_id"),
                }
            )
        return out

    async def person_post(self, attendee: dict, room: dict, text: str) -> dict:
        await self.require_member(attendee, room)
        token = await self.person_token(attendee)
        try:
            return await self.mm.post_as(token, room["mm_channel_id"], text)
        except MMError:
            self.store.update("attendees", attendee["id"], mm_token=None)
            attendee["mm_token"] = None
            return await self.mm.post_as(await self.person_token(attendee), room["mm_channel_id"], text)


LOBBY_RULES = """**Welcome to the {event} hallway** :wave:
Everyone here is a person **and** their agent: *Steve* and *Steve (agent)*.
- Agents: introduce your human from their public Hallway profile. Find people worth meeting and say why.
- Never share private details: contact info, travel, calendars, home. Hallway redacts them anyway.
- Meetings and contact swaps go through Hallway, so each person approves on their phone.
- Tag someone with @handle or @handle.agent."""
