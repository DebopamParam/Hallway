#!/usr/bin/env bash
# Restart the Hallway dev server. Usage: scripts/dev.sh [--reset]  (--reset wipes the local DB)
cd "$(dirname "$0")/.."
[ -f data/server.pid ] && kill "$(cat data/server.pid)" 2>/dev/null && sleep 1
[ "$1" = "--reset" ] && rm -f data/hallway.db*
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787 > data/server.log 2>&1 &
echo $! > data/server.pid
for i in $(seq 1 30); do curl -sf localhost:8787/health >/dev/null && echo "up (pid $(cat data/server.pid))" && exit 0; sleep 1; done
echo "failed to start"; tail -30 data/server.log; exit 1
