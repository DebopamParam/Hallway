# AGENTS.md: deploying and working on Hallway

Instructions for coding agents (Claude Code, Codex, any agent with a shell) that need to
run, deploy or change Hallway on a fresh machine without a human. People should start
with the [README](README.md); the full VPS walkthrough is [docs/DEPLOY.md](docs/DEPLOY.md),
running the event is [docs/ORGANISER.md](docs/ORGANISER.md), and how the parts connect is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Follow the steps in order. Run every command from the repository root. Never commit
`.env`, `deploy/switch/.env`, `data/` or any key, token or invite code.

## 1. Prerequisites

| Need | For | Check |
|---|---|---|
| git | cloning | `git --version` |
| Docker with the compose plugin | the Docker path (Switch, Mattermost, Postgres, Hallway) | `docker compose version` |
| curl, openssl, python3 | `scripts/up.sh` | `curl --version && openssl version && python3 --version` |
| Python 3.13+ and pip or [uv](https://docs.astral.sh/uv/) | the non-Docker path, tests, linters (CI uses 3.13; the images use 3.15) | `python3 --version` |
| Node.js (npx) | eslint only | `npx --version` |
| x86-64 (amd64) host | Mattermost ships amd64 images only | `uname -m` → `x86_64` |

Memory: the whole stack (Switch, Mattermost, Postgres, Hallway) fits on an 8 GB machine.

## 2. Clone

```bash
git clone https://github.com/DebopamParam/Hallway.git
cd Hallway
```

## 3. Environment variables

Hallway reads `.env` in the repo root (values already in the environment win). The
template, with every variable and its default, is [`.env.example`](.env.example);
`scripts/up.sh` writes `.env` for you. Generate each secret with `openssl rand -hex 24`.

| Variable | Required | Where the value comes from |
|---|---|---|
| `SWITCH_URL` | yes | Switch API base URL. `up.sh` / compose: `http://switch:8000`; local: `http://localhost:8000` |
| `SWITCH_REGISTRATION_KEY` | yes | Switch Gateway → Settings → API keys (an agent registration key). `up.sh` creates it through the Gateway API |
| `GATEWAY_ADMIN_EMAIL`, `GATEWAY_ADMIN_PASSWORD` | yes (for `enable-guard`) | The Switch Gateway admin login. `up.sh` generates it in `deploy/switch/.env` |
| `MM_URL`, `MM_ADMIN_USER`, `MM_ADMIN_PASSWORD`, `MM_TEAM` | yes | Mattermost (Switch's chat backend) admin login; team defaults to `switch`. `up.sh` generates them |
| `GUARD_API_KEY` | yes | Random secret. Switch sends it to `/guardrails/check` |
| `HALLWAY_ADMIN_KEY` | yes | Random secret. The organiser's key (`x-hallway-admin`) for `/api/admin/*`, `scripts/hallway_admin.py` and persona seeding |
| `DAYPASS_WORKER_KEY` | yes | Random secret. The day pass worker's key |
| `HALLWAY_PUBLIC_URL` | yes | The URL attendees open (`http://localhost:8787` locally, `https://your.domain` in production) |
| `HALLWAY_DB` | optional | SQLite path, default `./data/hallway.db` (compose: `/data/hallway.db`) |
| `HALLWAY_GUARD_ENDPOINT` | optional | Where Switch reaches Hallway's guard, default `http://hallway:8787` |
| `ORGANISER_PASSWORD` | optional | Organiser dashboard sign-in, at least 12 characters. Unset: `HALLWAY_ADMIN_KEY` |
| `ANTHROPIC_API_KEY` | optional | [Anthropic Console](https://console.anthropic.com). Day pass model when `DAYPASS_MODEL` is a Claude model |
| `VERCEL_AI_GATEWAY_API_KEY` | optional | Vercel AI Gateway. Day pass model for the default `DAYPASS_MODEL` |
| `EXA_API_KEY` | optional | [Exa](https://exa.ai). Web lookups for `research_people`; empty means profile-only ranking |
| `DAYPASS_ENABLED`, `DAYPASS_MODEL`, `DAYPASS_CAP_USD`, `DAYPASS_PRICE`, … | optional | Day pass settings, see `.env.example` |
| `EVENT_NAME`, `MAX_ATTENDEES` | optional | The event; also editable in the organiser dashboard |
| `INVITE_CODE`, `INVITE_LIMIT` | optional | Invite-only sign-up (`/?invite=<code>`) and a cap on attendees. `up.sh --demo` generates the code |
| `SIGNIN_MODE`, `EMAIL_SENDER`, `SMTP_*`, `RESEND_API_KEY` | optional | How sign-in links reach people: `dev` / `demo` (shown on screen) or `email` |
| `HALLWAY_ENV` | optional | `dev` or `production` |
| `TUNNEL_TOKEN` | optional | Cloudflare Zero Trust → Tunnels, for the `tunnel` compose profile (docs/DEPLOY.md) |
| `SWITCH_NETWORK` | optional | Docker network of an existing Switch project, default `switch_default` |

On any non-localhost `HALLWAY_PUBLIC_URL`, Hallway refuses to start if `GUARD_API_KEY`,
`HALLWAY_ADMIN_KEY` or `DAYPASS_WORKER_KEY` is empty, `change-me`, or the same as another.

## 4a. Docker path (recommended: everything, one command)

```bash
scripts/up.sh --help     # options
scripts/up.sh            # Switch 0.31.0 + Hallway, all on 127.0.0.1; safe to re-run
```

It starts Switch's standalone stack (vendored in `deploy/switch/`), generates its secrets
once, creates Hallway's registration key, writes `.env`, starts Hallway, and points Switch
Trust at Hallway's guard. Ports: Switch API 8000, Gateway 3000, Mattermost 8065, Postgres
5432, Hallway 8787; override with `API_HOST_PORT`, `GATEWAY_HOST_PORT`,
`MATTERMOST_HOST_PORT`, `POSTGRES_HOST_PORT`, `HALLWAY_PORT`.

Day pass worker too: set `DAYPASS_ENABLED=true` and a model key in `.env`, then

```bash
docker compose --profile daypass up -d --build
```

Public, invite-only deployment behind a Cloudflare Tunnel:
`scripts/up.sh --demo --host your.domain [--personas]`, then follow
[docs/DEPLOY.md](docs/DEPLOY.md) from the tunnel step.

**Switch already running elsewhere:**

```bash
cp .env.example .env
# fill in SWITCH_REGISTRATION_KEY, GATEWAY_ADMIN_*, MM_ADMIN_*, set SWITCH_NETWORK,
# and set GUARD_API_KEY, HALLWAY_ADMIN_KEY, DAYPASS_WORKER_KEY to three different `openssl rand -hex 24`
docker compose up -d --build
set -a; . ./.env; set +a
curl -X POST -H "x-hallway-admin: $HALLWAY_ADMIN_KEY" localhost:8787/api/admin/enable-guard
```

## 4b. Non-Docker path

Hallway itself runs from a virtualenv; it still needs a reachable Switch and Mattermost
(start them with `scripts/up.sh` or point `SWITCH_URL` / `MM_URL` at an existing stack).

```bash
uv venv && uv pip install -r requirements.txt          # or: python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
cp .env.example .env                                    # then fill it in (section 3)
scripts/dev.sh                                          # uvicorn on 127.0.0.1:8787; --reset wipes the local DB
```

`scripts/dev.sh` writes `data/server.log` and `data/server.pid` and waits for `/health`.
Day pass worker without Docker: `cd daypass && python worker.py` (install
`daypass/requirements.txt` first).

**No Switch at all (UI work, smoke checks):** `scripts/fake_stack.py` serves the real app
against the in-memory Switch and Mattermost fakes from `tests/fakes.py`, with a temp DB and
on-screen sign-in links. CI's accessibility job uses it.

```bash
.venv/bin/python scripts/fake_stack.py 8799 &
curl -sf http://localhost:8799/health
```

## 5. Create the event and invites

The event's name and day pass defaults come from `.env` (`EVENT_NAME`, `DAYPASS_*`) and can
be changed later in the organiser dashboard. Invites and attendees are managed with
`scripts/hallway_admin.py`, which talks to Hallway over HTTP with `HALLWAY_ADMIN_KEY`
(from the environment or `./.env`). The URL is `--url`, else `HALLWAY_URL`, else
`http://localhost:8787`.

```bash
.venv/bin/python scripts/hallway_admin.py --help
.venv/bin/python scripts/hallway_admin.py spots                     # invite code, link, spots left
.venv/bin/python scripts/hallway_admin.py invite --rotate           # a new random invite code; the old link stops working
.venv/bin/python scripts/hallway_admin.py attendees                 # who joined, mode, agent status
.venv/bin/python scripts/hallway_admin.py pass HANDLE --cap 0.50    # set a day pass budget (--pause / --resume)
.venv/bin/python scripts/hallway_admin.py export attendees -o attendees.csv
.venv/bin/python scripts/hallway_admin.py announce "Talks resume at 2pm"
.venv/bin/python scripts/hallway_admin.py --url https://your.domain spots   # against a deployed instance
```

With no `INVITE_CODE`, sign-up is open to anyone with the URL; `invite --rotate` turns the
invite gate on. Send the printed invite link to attendees privately; never commit it.
On the Docker path the image holds only `app/`, so run the script from the host (its port is
bound to `127.0.0.1:8787`) after installing `requirements.txt` in a venv, or against the public URL
with `--url`.

## 6. Organiser dashboard

A loopback-only web page (attendees, agent status, day pass spend, passes, event settings,
model keys). It runs Hallway itself plus the dashboard in one process, so use it **in place
of** `scripts/dev.sh` / `uvicorn` on the non-Docker path:

```bash
echo "ORGANISER_PASSWORD=$(openssl rand -hex 16)" >> .env    # or it signs in with HALLWAY_ADMIN_KEY
.venv/bin/python -m app.organiser --help
.venv/bin/python -m app.organiser                            # Hallway on :8787, dashboard on http://127.0.0.1:8788
```

The dashboard refuses a non-loopback host. From another machine:
`ssh -L 8788:127.0.0.1:8788 user@server`, then open `http://127.0.0.1:8788`. The Docker
image runs plain `uvicorn app.main:app`, so with the Docker path use `hallway_admin.py`
(section 5) or run `python -m app.organiser` on the host against the same `.env`.

## 7. Verify (health checks)

```bash
curl -sf http://localhost:8787/health          # {"ok":true,"guard":{...}}
docker compose ps                              # Docker path: hallway is "healthy"
curl -sf http://127.0.0.1:8788/ >/dev/null && echo dashboard up     # if app.organiser is running
```

`/health` returns `{"ok": true, ...}` with the guard's counters. Then open
`HALLWAY_PUBLIC_URL`, sign up (in `dev` sign-in mode the link is shown on screen) and
check the lobby appears.

## 8. Tests and linters

```bash
uv pip install -r requirements.txt -r requirements-dev.txt    # or: pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m pytest                                              # offline, in-process fakes, ignores .env
python -m compileall -q app daypass scripts tests
ruff check . && ruff format --check . && mypy
npx --yes eslint@10.13.0
```

Accessibility audit (as CI runs it):

```bash
python -m playwright install chromium
python scripts/fake_stack.py 8799 &
python scripts/a11y.py /tmp/a11y http://localhost:8799
```

CI (`.github/workflows/ci.yml`) runs all of the above plus both Docker builds
(`docker build -t hallway:ci .` and `docker build -t hallway-daypass:ci daypass`) on every
push and pull request. Dependencies are hash-locked: change `requirements*.in`, then
regenerate with `pip-compile --generate-hashes --allow-unsafe --strip-extras <file>.in`.

## 9. Common failures and fixes

| Symptom | Fix |
|---|---|
| Hallway exits at start complaining about a key | On a non-localhost `HALLWAY_PUBLIC_URL`, set `GUARD_API_KEY`, `HALLWAY_ADMIN_KEY`, `DAYPASS_WORKER_KEY` to three different random values |
| Sign-in mode `dev` refuses to start | `dev` only runs on a localhost URL or with `HALLWAY_ENV=dev`; use `demo` or `email` in production |
| `Application startup failed` after an `httpx.ConnectError` | Hallway (and `python -m app.organiser`) needs Switch and Mattermost reachable at start: check `SWITCH_URL`, `MM_URL` and that the Switch stack is up. For UI work without Switch, use `scripts/fake_stack.py` |
| `hallway_admin.py: set HALLWAY_ADMIN_KEY` | Export it or put it in `./.env` |
| `403` from `/api/admin/*` | Wrong or missing `x-hallway-admin`; must match the server's `HALLWAY_ADMIN_KEY` |
| `403` on app writes from a script | Send an `Origin` matching `HALLWAY_PUBLIC_URL` (see `scripts/signup.py`) |
| `scripts/dev.sh` prints `failed to start` | Read the tail of `data/server.log` it prints; usually Switch or Mattermost unreachable, or port 8787 in use |
| Messages aren't filtered by the guard | Run `enable-guard` (section 4a); check Switch can reach `HALLWAY_GUARD_ENDPOINT` |
| `docker compose up` can't find the network | Set `SWITCH_NETWORK` to your Switch project's `<name>_default` network |
| Mattermost image fails on ARM | Use an x86-64 host |
| Port already in use | Override `HALLWAY_PORT` / `API_HOST_PORT` / … before `scripts/up.sh` |
| Day pass agents don't run | `DAYPASS_ENABLED=true`, a model key set, and the worker started (`docker compose --profile daypass up -d`) |
| `429` with `Retry-After` | A rate limit; wait, or tune `RATE_LIMIT_*` in `.env` |

More troubleshooting for the VPS deployment: [docs/DEPLOY.md](docs/DEPLOY.md).

## 10. Connect an attendee's own agent (MCP)

After signing up, an attendee opens the **My agent** tab, which shows one setup prompt with
their personal key for Claude Code or Codex. Pasting it into their agent:

1. adds the Hallway MCP server at `<HALLWAY_PUBLIC_URL>/mcp` with a bearer token:
   - Claude Code: `claude mcp add -s user --transport http hallway <HALLWAY_PUBLIC_URL>/mcp --header "Authorization: Bearer <token>"`
   - Codex: a `[mcp_servers.hallway]` section in `~/.codex/config.toml`
2. starts the watcher, `node ~/.hallway-agent/hallway-watch.mjs claude|codex --background`,
   downloaded from `<HALLWAY_PUBLIC_URL>/watch.mjs`. It long-polls `/agent/events` and runs
   the agent headless when something arrives. Log: `~/.hallway-agent/watch.log`.

Use the exact prompt from the app rather than building it by hand; the token is personal and
must never be shared or committed. Tool list and behaviour:
[README → Connect an agent](README.md#connect-an-agent-bring-your-own-via-mcp).

## Working on the code

- Layout: see [README → Layout](README.md#layout). The attendee app (`app/static/`) has no build step.
- Keep the test suite offline: new tests use the fakes in `tests/fakes.py`.
- Run tests, ruff, mypy and eslint (section 8) before opening a pull request.
