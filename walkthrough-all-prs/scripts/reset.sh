#!/bin/bash
# Fresh fake stacks, seeded crowd, stand-in workers on 8896 (main) and 8897 (after).
W=/tmp/claude-1000/qa2/walk
cd $W
[ -f out/stack.pids ] && kill $(cat out/stack.pids) 2>/dev/null
sleep 2
. tools/keys.env
export DAYPASS_ENABLED=1 DAYPASS_PRICE=Free
for pair in main:8896 after:8897; do n=${pair%:*}; port=${pair#*:}
  (cd $W/$n && setsid nohup .venv/bin/python scripts/fake_stack.py $port > $W/out/stack-$n.log 2>&1 &)
done
sleep 5
for pair in main:8896 after:8897; do n=${pair%:*}; port=${pair#*:}
  HALLWAY_URL=http://localhost:$port $n/.venv/bin/python tools/seed.py http://localhost:$port $W/$n | tail -1
  rm -f out/worker-$n.jsonl
  setsid nohup $n/.venv/bin/python tools/worker.py http://localhost:$port out/worker-$n.jsonl > out/worker-$n.err 2>&1 &
done
sleep 1
pgrep -f "fake_stack.py 889[67]|tools/worker.py" | tr '\n' ' ' > out/stack.pids
