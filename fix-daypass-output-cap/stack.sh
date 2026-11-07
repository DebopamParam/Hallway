#!/bin/bash
# stack.sh PORT TREE : fresh fake stack on PORT from TREE, 5 Connect personas seeded.
S=$(cd "$(dirname "$0")" && pwd); PORT=$1; TREE=$2; AK=$(sed -n 1p $S/keys); WK=$(sed -n 2p $S/keys)
for pid in $(pgrep -f "fake_stack.py $PORT"); do kill $pid; done; sleep 1
(cd $TREE && exec env DAYPASS_ENABLED=1 DAYPASS_PRICE=Free HALLWAY_ADMIN_KEY=$AK DAYPASS_WORKER_KEY=$WK nohup .venv/bin/python scripts/fake_stack.py $PORT > $S/stack-$PORT.log 2>&1 & echo $! > $S/stack-$PORT.pid); sleep 5
for i in 0 1 2 3 4; do HALLWAY_URL=http://localhost:$PORT HALLWAY_ADMIN_KEY=$AK /home/agent/workspace/hallway/.venv/bin/python $TREE/scripts/signup.py $S/persona$i.json >/dev/null 2>&1 || echo seed fail $i; done
echo "stack $PORT pid $(cat $S/stack-$PORT.pid), due: $(curl -s -H "x-hallway-worker: $WK" "localhost:$PORT/internal/daypass/due?wait=0")"
