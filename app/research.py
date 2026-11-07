"""Web lookups for research_people: what the public web says about an attendee,
through Exa (https://exa.ai), paid for by the organiser (EXA_API_KEY, or the key set in the organiser dashboard).

What an agent gets back is a few public pages per person (title, URL, a short
summary), never raw page text, and every field has been through the privacy
guard first: an email or phone number printed on someone's homepage comes back
redacted, as it would in a room. Attendees can opt out (no lookup at all), the
demo personas are never looked up (a namesake's pages would be wrong), and
lookups are cached and counted against a daily budget.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from . import guard, providers
from .config import settings

log = logging.getLogger("hallway")

EXA_URL = "https://api.exa.ai/search"
SOURCES_PER_PERSON = 3
SUMMARY_QUERY = "What does this person do professionally: role, company, focus, notable work?"
CACHE_S = 6 * 3600  # a person's pages don't change during an event
FIELD_MAX = {"title": 120, "summary": 400}


class LookupError(Exception):
    """A lookup that didn't happen; `reason` is safe to show the agent."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def api_key() -> str:
    """The dashboard's key (app/providers.py), else EXA_API_KEY; read per lookup, so a new key needs no restart."""
    return providers.saved("EXA_API_KEY") or settings.exa_api_key


def enabled() -> bool:
    return bool(api_key())


def query_for(a: dict) -> str:
    """Name plus whatever public profile fields pin down which person it is."""
    p = a["profile"]
    parts = [a["name"], p.get("role", ""), p.get("company", "")]
    return " ".join(x for x in parts if x).strip()


def _clean(text: Any, limit: int, private: guard.PrivateMatcher | None) -> str:
    text = " ".join(str(text or "").split())[:limit]
    outcome, findings = guard.scan(text, private)
    return guard.redact(text, findings) if outcome == "redacted" else text


def shape(results: list[dict], private: guard.PrivateMatcher | None) -> list[dict]:
    """Exa results -> what an agent may see: title, url, summary; all guarded."""
    out = []
    for r in results[:SOURCES_PER_PERSON]:
        url = str(r.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        out.append(
            {
                "title": _clean(r.get("title"), FIELD_MAX["title"], private),
                "url": url[:300],
                "summary": _clean(
                    r.get("summary") or " ".join(r.get("highlights") or []), FIELD_MAX["summary"], private
                ),
            }
        )
    return out


async def exa_search(query: str) -> list[dict]:
    body = {
        "query": query,
        "numResults": SOURCES_PER_PERSON,
        "type": "auto",
        "contents": {"summary": {"query": SUMMARY_QUERY}},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(EXA_URL, json=body, headers={"x-api-key": api_key()})
    except httpx.HTTPError as e:
        log.warning("exa: %s", e)
        raise LookupError("The web search provider didn't answer. Try again later.") from e
    if r.status_code != 200:
        log.warning("exa: HTTP %s: %s", r.status_code, r.text[:200])
        raise LookupError("The web search provider refused the lookup. Try again later.")
    data = r.json()
    log.info(
        "exa: %r -> %d results ($%s)", query, len(data.get("results", [])), (data.get("costDollars") or {}).get("total")
    )
    return list(data.get("results") or [])


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def lookable(a: dict) -> str | None:
    """Why `a` may not be looked up, or None when they may."""
    if a["is_persona"]:
        return "demo persona (made up, so the web has nothing on them)"
    if not a["listed"]:
        return "private profile"
    if not a["web_lookup"]:
        return "opted out of web lookups"
    return None


def cached(store: Any, a: dict) -> list[dict] | None:
    hit = store.kv_get(f"web:{a['id']}")
    if hit and hit.get("query") == query_for(a) and time.time() - hit.get("at", 0) < CACHE_S:
        return list(hit["sources"])
    return None


def _spend(store: Any, requester_id: str) -> None:
    """Count one uncached lookup against today's budgets, or refuse it."""
    day = today()
    total = store.kv_get(f"research:{day}") or 0
    mine = store.kv_get(f"research:{day}:{requester_id}") or 0
    if total >= settings.research_daily_cap:
        raise LookupError("Today's web lookup budget for the event is used up; ranking by Hallway profiles only.")
    if mine >= settings.research_attendee_cap:
        raise LookupError("You've used today's web lookups; ranking by Hallway profiles only.")
    store.kv_set(f"research:{day}", total + 1)
    store.kv_set(f"research:{day}:{requester_id}", mine + 1)


async def lookup(store: Any, a: dict, requester_id: str, private: guard.PrivateMatcher | None) -> list[dict]:
    """What the public web says about attendee `a`: cached for CACHE_S, else one
    Exa search counted against the requester's and the event's daily budget."""
    why = lookable(a)
    if why:
        raise LookupError(f"Not looked up: {why}.")
    if not enabled():
        raise LookupError("Web lookups aren't set up at this event.")
    hit = cached(store, a)
    if hit is not None:
        return hit
    _spend(store, requester_id)
    sources = shape(await exa_search(query_for(a)), private)
    store.kv_set(f"web:{a['id']}", {"at": time.time(), "query": query_for(a), "sources": sources})
    return sources
