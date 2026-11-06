#!/usr/bin/env bash
# Wipe Hallway's demo data for a fresh demo, then bring the demo back up:
#   scripts/demo-reset.sh [--yes] [--new-invite] [--personas]
#     --yes         don't ask for confirmation
#     --new-invite  also make a new INVITE_CODE (the old invite link stops working)
#     --personas    sign the demo personas up again afterwards
#
# Deleted: Hallway's data volume (every attendee, session, sign-in link, room
# record, approval and day pass, and the invite count, so all INVITE_LIMIT
# spots are free again) and data/personas.state.json.
# Kept: .env (keys, invite code unless --new-invite, TUNNEL_TOKEN), the whole
# Switch stack and its config (deploy/switch/.env, Postgres, Mattermost). Old
# attendees' Mattermost users and the old lobby channel stay in Switch, unused;
# Hallway makes a new lobby on start.
set -euo pipefail
cd "$(dirname "$0")/.."

HW_PROJECT=${HALLWAY_PROJECT:-hallway}
YES=0 NEW_INVITE=0 up_args=(--demo)
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --new-invite) NEW_INVITE=1 ;;
    --personas) up_args+=(--personas) ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "demo-reset.sh: unknown option $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

[ -f .env ] || { echo "No .env here: nothing to reset (run scripts/up.sh --demo first)." >&2; exit 1; }
volume="${HW_PROJECT}_hallway-data"
if [ "$YES" != 1 ]; then
  echo "This deletes every Hallway attendee, session and room record (volume $volume)"
  echo "and frees all invite spots. Switch, Mattermost and .env are kept."
  read -r -p "Type 'reset' to go on: " answer
  [ "$answer" = reset ] || { echo "Nothing changed."; exit 1; }
fi

printf '\033[1m==> %s\033[0m\n' "Stopping Hallway (project $HW_PROJECT)"
docker compose -p "$HW_PROJECT" --profile daypass --profile tunnel down
if docker volume inspect "$volume" >/dev/null 2>&1; then
  docker volume rm "$volume" >/dev/null
  echo "Deleted volume $volume"
fi
rm -f data/personas.state.json
if [ "$NEW_INVITE" = 1 ]; then
  python3 - .env "$(openssl rand -hex 8)" <<'PY'
import re, sys
path, code = sys.argv[1:]
text = open(path, encoding="utf-8").read()
text = re.sub(r"^INVITE_CODE=.*$", f"INVITE_CODE={code}", text, count=1, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PY
  echo "New invite code: the old invite link no longer works"
fi

exec scripts/up.sh "${up_args[@]}"
