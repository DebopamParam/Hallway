#!/bin/bash
# run_worker.sh PORT TREE LOG : one pass (--once) of the real day pass worker from TREE against localhost:PORT only.
S=$(cd "$(dirname "$0")" && pwd); PORT=$1; TREE=$2; LOG=$3
GW=$(grep -E '^VERCEL_AI_GATEWAY_API_KEY=' /home/agent/workspace/hallway/.env | cut -d= -f2- | tr -d "\"'")
cd "$S" && exec env -i PATH=/usr/bin:/bin HOME=$HOME \
  HALLWAY_URL=http://127.0.0.1:$PORT DAYPASS_WORKER_KEY=$(sed -n 2p $S/keys) \
  HALLWAY_PROVIDERS_FILE=$S/no-providers.json \
  DAYPASS_MODEL=vercel:deepseek/deepseek-v4.1-flash-1017 DAYPASS_PRICE_PER_MTOK=0.30,1.20 \
  VERCEL_AI_GATEWAY_API_KEY="$GW" \
  /home/agent/workspace/hallway/daypass/.venv/bin/python -I $S/run_logged.py "$TREE" >> "$LOG" 2>&1
