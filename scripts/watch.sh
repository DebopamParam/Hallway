#!/usr/bin/env bash
# Follow Hallway's logs and print (optionally push) only what needs a human:
#   scripts/watch.sh                         follow the hallway container, print problems
#   scripts/watch.sh --notify URL            also POST each one to URL (ntfy.sh topic, Slack/Discord webhook)
#   scripts/watch.sh --since 1h              start with the last hour instead of now
#   some-command | scripts/watch.sh --stdin  filter any log stream
#
# A problem is an ERROR/CRITICAL line or traceback (text or LOG_FORMAT=json),
# the guard failing or running slow (Switch delivers unchecked after 2 s), a
# wrong guard key, a database error, or a 5xx in the access log. Pushes are
# throttled to one per --every seconds (default 300); the rest are counted and
# mentioned in the next one. Runs until stopped; under tmux, or nohup:
#   nohup scripts/watch.sh --notify https://ntfy.sh/<topic> > watch.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."

HW_PROJECT=${HALLWAY_PROJECT:-hallway}
SINCE=0s NOTIFY="" EVERY=300 STDIN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE=${2:?--since needs a duration, e.g. 1h}; shift ;;
    --notify) NOTIFY=${2:?--notify needs a URL}; shift ;;
    --every) EVERY=${2:?--every needs seconds}; shift ;;
    --stdin) STDIN=1 ;;
    -h|--help) sed -n '2,14p' scripts/watch.sh; exit 0 ;;
    *) echo "watch.sh: unknown option $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

PATTERN='( ERROR | CRITICAL |"level": ?"(error|critical)"|Traceback \(most recent call last\)|guard: (check failed|rejected a check|a check took|over RATE_LIMIT_GUARD|could not write)|database is locked|disk I/O error|" (5[0-9][0-9])( |$)|"status": ?5[0-9][0-9])'

source_logs() {
  if [ "$STDIN" = 1 ]; then
    cat
  else
    docker compose -p "$HW_PROJECT" logs --no-color --no-log-prefix --follow --since "$SINCE" hallway
  fi
}

last_push=0 held=0
notify() {
  [ -n "$NOTIFY" ] || return 0
  local now
  now=$(date +%s)
  if (( now - last_push < EVERY )); then
    held=$((held + 1))
    return 0
  fi
  local text="Hallway: $1"
  (( held > 0 )) && text="$text (+$held more since the last alert)"
  curl -fsS -m 10 -X POST -H "Content-Type: text/plain" --data-binary "$text" "$NOTIFY" > /dev/null \
    || echo "watch.sh: could not push the alert to --notify" >&2
  last_push=$now held=0
}

source_logs | grep --line-buffered -E "$PATTERN" | while IFS= read -r line; do
  echo "$(date -u +%FT%TZ) $line"
  notify "${line:0:300}"
done || true  # grep finding nothing in a finished stream isn't a failure
