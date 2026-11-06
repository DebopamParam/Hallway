#!/usr/bin/env bash
# Fresh clone to a running Switch + Hallway, in one command:
#   scripts/up.sh                 # Switch on :8000/:3000/:8065, Hallway on :8787
#   API_HOST_PORT=18000 ... scripts/up.sh   # other ports, see the defaults below
# Safe to re-run: secrets and keys are made once and kept in deploy/switch/.env
# and .env. Needs docker (with compose), curl, openssl and python3.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${COMPOSE_PROJECT_NAME:-switch}
API_PORT=${API_HOST_PORT:-8000}
GW_PORT=${GATEWAY_HOST_PORT:-3000}
MM_PORT=${MATTERMOST_HOST_PORT:-8065}
PG_PORT=${POSTGRES_HOST_PORT:-5432}
HW_PORT=${HALLWAY_PORT:-8787}
HW_PROJECT=${HALLWAY_PROJECT:-hallway}
SW=deploy/switch
API=http://127.0.0.1:$API_PORT

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
rand() { openssl rand -hex "${1:-24}"; }
# setenv FILE KEY VALUE: replace KEY's line, or append it
setenv() { python3 - "$@" <<'PY'
import sys, re
path, key, value = sys.argv[1:]
text = open(path, encoding="utf-8").read()
line = f"{key}={value}"
pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
text = pat.sub(lambda _: line, text, count=1) if pat.search(text) else text.rstrip("\n") + f"\n{line}\n"
open(path, "w", encoding="utf-8").write(text)
PY
}
getenv() { grep "^$2=" "$1" | tail -1 | cut -d= -f2-; }
wait_for() {  # wait_for NAME URL [SECONDS]
  for _ in $(seq "${3:-180}"); do curl -fs -o /dev/null "$2" && return 0; sleep 1; done
  echo "$1 did not come up at $2" >&2; exit 1
}

# 1. Switch's .env: generated once, never regenerated (the secrets encrypt data)
[ -f $SW/.env ] || { say "Generating Switch secrets ($SW/.env)"; cp $SW/env.template $SW/.env; }
for key in $(grep '=__RANDOM__$' $SW/env.template | cut -d= -f1); do
  case "$(getenv $SW/.env "$key")" in ""|__RANDOM__) setenv $SW/.env "$key" "$(rand)";; esac
done
case "$(getenv $SW/.env SECRET_KEYS)" in ""|__SECRET_KEYS__) setenv $SW/.env SECRET_KEYS "hallway:$(rand 32)";; esac
for kv in COMPOSE_PROJECT_NAME=$PROJECT API_HOST_PORT=$API_PORT GATEWAY_HOST_PORT=$GW_PORT \
          MATTERMOST_HOST_PORT=$MM_PORT POSTGRES_HOST_PORT=$PG_PORT \
          GATEWAY_PUBLIC_URL=http://localhost:$API_PORT FRONTEND_BASE_URL=http://localhost:$GW_PORT; do
  setenv $SW/.env "${kv%%=*}" "${kv#*=}"
done

say "Starting Switch 0.31.0 (project $PROJECT)"
docker compose -p "$PROJECT" -f $SW/docker-compose.yml up -d
wait_for Switch $API/health
wait_for Mattermost http://127.0.0.1:$MM_PORT/api/v4/system/ping
# Hallway's lobby must be created after Switch's seeder registers the Mattermost
# bridge, or the room comes out unbridged and nobody can join it
for _ in $(seq 180); do
  state=$(docker compose -p "$PROJECT" -f $SW/docker-compose.yml ps -a setup --format '{{.State}} {{.ExitCode}}')
  [ "$state" = "exited 0" ] && break; sleep 1
done
[ "$state" = "exited 0" ] || { echo "Switch setup did not finish ($state); see: docker logs $PROJECT-setup-1" >&2; exit 1; }

# 2. Hallway's .env, with a registration key made through the Gateway API
[ -f .env ] || { say "Writing .env from .env.example"; cp .env.example .env; }
ADMIN_EMAIL=$(getenv $SW/.env GATEWAY_ADMIN_EMAIL)
ADMIN_PASS=$(getenv $SW/.env GATEWAY_ADMIN_PASSWORD)
if [ -z "$(getenv .env SWITCH_REGISTRATION_KEY)" ]; then
  say "Creating a Switch registration key for Hallway"
  jar=$(mktemp); trap 'rm -f "$jar"' EXIT
  for _ in $(seq 60); do  # the admin account is seeded shortly after /health
    curl -fs -c "$jar" -H 'content-type: application/json' -o /dev/null \
      -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASS\"}" $API/gateway/auth/login && break
    sleep 2
  done
  key=$(curl -fs -b "$jar" -H 'content-type: application/json' -d '{"label":"hallway"}' \
    $API/gateway/api-keys | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
  setenv .env SWITCH_REGISTRATION_KEY "$key"
fi
# Three separate secrets, made once: Switch's guard calls, the organiser's admin
# key, the day pass worker's key. A leak of one doesn't expose the others.
for key in GUARD_API_KEY HALLWAY_ADMIN_KEY DAYPASS_WORKER_KEY; do
  case "$(getenv .env $key)" in ""|change-me) setenv .env $key "$(rand)";; esac
done
for kv in GATEWAY_ADMIN_EMAIL=$ADMIN_EMAIL GATEWAY_ADMIN_PASSWORD=$ADMIN_PASS \
          MM_ADMIN_USER=$(getenv $SW/.env MATTERMOST_ADMIN_USER) \
          MM_ADMIN_PASSWORD=$(getenv $SW/.env MATTERMOST_ADMIN_PASSWORD) \
          MM_TEAM=$(getenv $SW/.env MATTERMOST_TEAM_NAME) \
          SWITCH_URL=http://localhost:$API_PORT MM_URL=http://localhost:$MM_PORT \
          SWITCH_NETWORK=${PROJECT}_default HALLWAY_PORT=$HW_PORT; do
  setenv .env "${kv%%=*}" "${kv#*=}"
done
grep -q '^HALLWAY_PUBLIC_URL=http://localhost:' .env && setenv .env HALLWAY_PUBLIC_URL "http://localhost:$HW_PORT"

# 3. Hallway, then point Switch Trust at Hallway's privacy guard
say "Starting Hallway"
profiles=(); [ "$(getenv .env DAYPASS_ENABLED)" = true ] && [ -n "$(getenv .env VERCEL_AI_GATEWAY_API_KEY)$(getenv .env ANTHROPIC_API_KEY)" ] && profiles=(--profile daypass)
[ "${SKIP_BUILD:-}" = 1 ] || docker compose -p "$HW_PROJECT" "${profiles[@]}" build  # SKIP_BUILD=1: use images you built
# The image runs as uid 10001. A data volume made by an older, root image is
# owned by root: hand it over once (a no-op on a fresh volume).
if docker volume inspect "${HW_PROJECT}_hallway-data" >/dev/null 2>&1; then
  docker compose -p "$HW_PROJECT" run --rm --no-deps -T --user 0 --cap-add CHOWN --cap-add DAC_READ_SEARCH \
    --entrypoint chown hallway -R hallway:hallway /data
fi
docker compose -p "$HW_PROJECT" "${profiles[@]}" up -d
wait_for Hallway http://127.0.0.1:$HW_PORT/health
curl -fs -X POST -H "x-hallway-admin: $(getenv .env HALLWAY_ADMIN_KEY)" http://127.0.0.1:$HW_PORT/api/admin/enable-guard >/dev/null \
  && say "Switch Trust now checks every message with Hallway's guard"

cat <<DONE

  Hallway         http://localhost:$HW_PORT
  Switch Gateway  http://localhost:$GW_PORT   ($ADMIN_EMAIL, password in $SW/.env)

  Stop:  docker compose -p $HW_PROJECT down && docker compose -p $PROJECT -f $SW/docker-compose.yml down
DONE
