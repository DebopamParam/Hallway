#!/usr/bin/env bash
# Online backups of Hallway's SQLite database, with retention, and restore.
#
#   scripts/backup.sh [--keep N] [--out DIR]               back up the running container's DB
#   scripts/backup.sh --db PATH [--keep N] [--out DIR]     back up a DB file on this machine
#   scripts/backup.sh restore FILE                         put FILE back into the container's volume
#   scripts/backup.sh restore FILE --db PATH               put FILE back at PATH (stop the app first)
#   scripts/backup.sh check FILE                           integrity check + row counts
#
# A backup is a consistent snapshot taken with SQLite's backup API while
# Hallway keeps running (scripts/sqlite_backup.py): no downtime, no torn copy.
# It lands in DIR (default ./backups, or BACKUP_DIR) as
# hallway-YYYYMMDDTHHMMSSZ.db, is integrity-checked before it gets that name,
# and only the newest N (default 14, or BACKUP_KEEP) are kept.
#
# Restore checks FILE first, saves the current DB as a pre-restore backup,
# then (container mode) stops Hallway, writes FILE into the hallway-data
# volume through a one-off container and starts Hallway again.
#
# Container mode uses compose project HALLWAY_PROJECT (default hallway), or
# --container NAME for a container started some other way. Cron (docs/DEPLOY.md):
#   17 3 * * * cd ~/Hallway && scripts/backup.sh --keep 14 >> backups/backup.log 2>&1
set -euo pipefail
CALLER=$PWD
cd "$(dirname "$0")/.."
# Paths given on the command line are relative to where it was run from.
abs() { case "$1" in /*) echo "$1" ;; *) echo "$CALLER/$1" ;; esac; }

HW_PROJECT=${HALLWAY_PROJECT:-hallway}
OUT=${BACKUP_DIR:-backups}
KEEP=${BACKUP_KEEP:-14}
DB="" CONTAINER="" CMD=backup FILE=""
CONTAINER_DB=/data/hallway.db
HELPER=scripts/sqlite_backup.py

die() { echo "backup.sh: $*" >&2; exit 1; }
say() { echo "$(date -u +%FT%TZ) $*"; }

case "${1:-}" in
  restore|check) CMD=$1; FILE=$(abs "${2:?$1 needs a backup file}"); shift 2 ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --db) DB=$(abs "${2:?--db needs a path}"); shift ;;
    --container) CONTAINER=${2:?--container needs a name}; shift ;;
    --out) OUT=$(abs "${2:?--out needs a directory}"); shift ;;
    --keep) KEEP=${2:?--keep needs a number}; shift ;;
    -h|--help) sed -n '2,24p' "scripts/backup.sh"; exit 0 ;;
    *) die "unknown option $1 (try --help)" ;;
  esac
  shift
done
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "--keep must be a whole number of at least 1"

container() {
  [ -n "$CONTAINER" ] && { echo "$CONTAINER"; return; }
  local id
  id=$(docker compose -p "$HW_PROJECT" ps -q hallway 2>/dev/null || true)
  [ -n "$id" ] || die "no running hallway container in compose project $HW_PROJECT (or pass --db / --container)"
  echo "$id"
}

# Back up into DIR/NAME. Written as .partial and renamed once checked, so a
# half-written file never looks like a backup (and never counts towards KEEP).
take_backup() {
  local dir=$1 name=$2 tmp
  mkdir -p "$dir"
  tmp="$dir/$name.partial"
  rm -f "$tmp"
  if [ -n "$DB" ]; then
    python3 "$HELPER" backup "$DB" "$tmp"
  else
    docker exec -i "$(container)" python -c "$(cat "$HELPER")" backup "$CONTAINER_DB" - > "$tmp"
  fi
  python3 "$HELPER" check "$tmp" > /dev/null || { rm -f "$tmp"; die "backup failed its integrity check"; }
  chmod 600 "$tmp"  # attendee data: contact details, session hashes
  mv "$tmp" "$dir/$name"
}

prune() {
  local all=() i
  while IFS= read -r f; do all+=("$f"); done < <(ls -1 "$OUT"/hallway-*.db 2>/dev/null | sort)
  for ((i = 0; i < ${#all[@]} - KEEP; i++)); do
    rm -f "${all[i]}"
    say "pruned ${all[i]}"
  done
}

stamp() { date -u +%Y%m%dT%H%M%SZ; }

case "$CMD" in
  backup)
    name="hallway-$(stamp).db"
    take_backup "$OUT" "$name"
    say "backed up to $OUT/$name ($(wc -c < "$OUT/$name" | tr -d ' ') bytes): $(python3 "$HELPER" check "$OUT/$name")"
    prune
    ;;
  check)
    python3 "$HELPER" check "$FILE"
    ;;
  restore)
    [ -f "$FILE" ] || die "no file $FILE"
    python3 "$HELPER" check "$FILE" > /dev/null || die "$FILE is not a usable backup; nothing changed"
    # The current DB first, so a restore can itself be undone. Not pruned.
    safety="pre-restore-$(stamp).db"
    if [ -n "$DB" ]; then
      [ -f "$DB" ] && take_backup "$OUT" "$safety" && say "saved the current DB as $OUT/$safety"
      python3 "$HELPER" restore "$FILE" "$DB"
    else
      [ -z "$CONTAINER" ] || die "restore works on the compose project's volume; use --db for anything else"
      take_backup "$OUT" "$safety" && say "saved the current DB as $OUT/$safety"
      say "stopping hallway (project $HW_PROJECT)"
      docker compose -p "$HW_PROJECT" stop hallway
      # A one-off container on the same volume, as the image's user; the file
      # goes in on stdin, so nothing else of the host is mounted.
      docker compose -p "$HW_PROJECT" run --rm --no-deps -T --entrypoint python hallway \
        -c "$(cat "$HELPER")" restore - "$CONTAINER_DB" < "$FILE"
      say "starting hallway"
      docker compose -p "$HW_PROJECT" start hallway
    fi
    say "restored from $FILE"
    ;;
esac
