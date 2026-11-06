"""Hallway organiser CLI: run an event from a terminal, over HTTP with the admin key.

    scripts/hallway_admin.py [--url URL] COMMAND ...

    attendees [--json]              who joined: mode, agent status, day pass
    spots                           invite code and link, spots left
    invite --rotate [--code CODE]   a new invite code (random unless --code); the old link stops working
    remove HANDLE [--yes]           remove an attendee: deletes their Switch stand-in, deactivates
                                    their Mattermost user, frees their spot
    pass HANDLE [--pause | --resume] [--cap USD]
                                    pause or resume a day pass, and/or set its budget
    export attendees|meetings [-o FILE]
                                    CSV to FILE (default: stdout)
    announce TEXT                   post TEXT in the lobby as the host agent

The URL is --url, else HALLWAY_URL, else http://localhost:8787. Against the VPS,
use its public URL: the requests go through the tunnel like any visitor's. The
key is HALLWAY_ADMIN_KEY from the environment or ./.env (see signup.py); it is
sent as x-hallway-admin, which the admin endpoints require."""

import argparse
import json
import os
import sys
import time

import httpx
from signup import admin_key


def client(url: str) -> httpx.Client:
    key = admin_key()
    if not key:
        sys.exit("hallway_admin.py: set HALLWAY_ADMIN_KEY (environment or ./.env)")
    return httpx.Client(
        base_url=url.rstrip("/"),
        headers={"x-hallway-admin": key, "user-agent": "hallway-admin/1"},
        timeout=60,
    )


def call(c: httpx.Client, method: str, path: str, body: dict | None = None) -> httpx.Response:
    try:
        r = c.request(method, path, json=body)
    except httpx.HTTPError as e:
        sys.exit(f"hallway_admin.py: can't reach {c.base_url}: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json()
            msg = detail.get("error") or detail.get("detail") or r.text
        except ValueError:
            msg = r.text[:300]
        sys.exit(f"hallway_admin.py: {method} {path}: {r.status_code} {msg}")
    return r


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


def table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip() for r in rows)


def cmd_attendees(c: httpx.Client, a: argparse.Namespace) -> None:
    people = call(c, "GET", "/api/admin/attendees").json()
    if a.json:
        print(json.dumps(people, indent=2))
        return
    rows = [["HANDLE", "NAME", "MODE", "AGENT", "STATUS", "LAST SEEN", "DAY PASS"]]
    for p in people:
        dp = p["pass"]
        rows.append(
            [
                p["handle"],
                p["name"] + (" (persona)" if p["persona"] else ""),
                p["mode"],
                p["agent_name"],
                p["agent_status"],
                ago(p["agent_seen_at"]),
                f"{dp['status']} ${dp['spent_usd']:.2f}/${dp['cap_usd']:.2f}" if dp else "-",
            ]
        )
    print(table(rows))
    active = sum(p["agent_status"] == "active" for p in people)
    print(f"\n{len(people)} attendees, {active} agents active in the last 15 minutes")


def print_invite(inv: dict) -> None:
    if inv["invite_only"]:
        print(f"invite code: {inv['invite_code']}\ninvite link: {inv['invite_url']}")
    else:
        print("invite: open (no invite code: anyone with the URL can join)")
    limit = inv["invite_limit"]
    print(f"joined: {inv['joined']}" + (f" of {limit}, {inv['spots_left']} spots left" if limit else " (no limit)"))


def cmd_spots(c: httpx.Client, a: argparse.Namespace) -> None:
    print_invite(call(c, "GET", "/api/admin/invite").json())


def cmd_invite(c: httpx.Client, a: argparse.Namespace) -> None:
    if not a.rotate:
        return cmd_spots(c, a)
    inv = call(c, "POST", "/api/admin/invite/rotate", {"code": a.code} if a.code else {}).json()
    print("rotated: the old invite link no longer lets new people in")
    print_invite(inv)


def cmd_remove(c: httpx.Client, a: argparse.Namespace) -> None:
    if not a.yes:
        if not sys.stdin.isatty():
            sys.exit("hallway_admin.py: pass --yes to remove without a prompt")
        if input(f"Remove {a.handle} from the event? This can't be undone. [y/N] ").strip().lower() != "y":
            sys.exit("not removed")
    r = call(c, "DELETE", f"/api/admin/attendees/{a.handle}").json()
    print(f"removed {r['removed']}")
    print(f"  Switch agent:    {r['switch_agent']}")
    print(f"  Mattermost user: {r.get('mattermost_user', 'none')}")
    if r["spots_left"] is not None:
        print(f"  spots left:      {r['spots_left']}")


def cmd_pass(c: httpx.Client, a: argparse.Namespace) -> None:
    body: dict = {}
    if a.pause or a.resume:
        body["paused"] = bool(a.pause)
    if a.cap is not None:
        body["cap_usd"] = a.cap
    if not body:
        sys.exit("hallway_admin.py: pass --pause, --resume and/or --cap USD")
    r = call(c, "POST", f"/api/admin/attendees/{a.handle}/pass", body).json()
    print(f"{r['handle']}: {r['status']}, ${r['spent_usd']:.2f} spent of ${r['cap_usd']:.2f}")


def cmd_export(c: httpx.Client, a: argparse.Namespace) -> None:
    text = call(c, "GET", f"/api/admin/export/{a.what}.csv").text
    if a.output in (None, "-"):
        sys.stdout.write(text)
        return
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {max(0, text.count(chr(10)) - 1)} {a.what} to {a.output}", file=sys.stderr)


def cmd_announce(c: httpx.Client, a: argparse.Namespace) -> None:
    r = call(c, "POST", "/api/admin/announce", {"text": " ".join(a.text)}).json()
    print(f"posted in {r['room']} as the host agent")


def main() -> None:
    ap = argparse.ArgumentParser(description="Hallway organiser CLI", formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("HALLWAY_URL", "http://localhost:8787"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("attendees", help="list attendees with mode and agent status")
    p.add_argument("--json", action="store_true")
    sub.add_parser("spots", help="invite code, link and spots left")
    p = sub.add_parser("invite", help="show, or --rotate, the invite code")
    p.add_argument("--rotate", action="store_true")
    p.add_argument("--code", help="the new code (default: random)")
    p = sub.add_parser("remove", help="remove an attendee and free their spot")
    p.add_argument("handle")
    p.add_argument("--yes", action="store_true", help="don't ask")
    p = sub.add_parser("pass", help="pause/resume a day pass, set its cap")
    p.add_argument("handle")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pause", action="store_true")
    g.add_argument("--resume", action="store_true")
    p.add_argument("--cap", type=float, metavar="USD")
    p = sub.add_parser("export", help="attendees or meetings as CSV")
    p.add_argument("what", choices=["attendees", "meetings"])
    p.add_argument("-o", "--output", help="file to write (default: stdout)")
    p = sub.add_parser("announce", help="post in the lobby as the host agent")
    p.add_argument("text", nargs="+")
    a = ap.parse_args()
    if a.cmd == "invite" and a.code and not a.rotate:
        ap.error("--code goes with --rotate")
    with client(a.url) as c:
        globals()[f"cmd_{a.cmd}"](c, a)


if __name__ == "__main__":
    main()
