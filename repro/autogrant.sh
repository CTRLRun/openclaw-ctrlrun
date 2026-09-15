#!/bin/zsh
# Grant any held plugin approval the instant it appears.
#
# A person at a TUI answers in seconds; polling the CLI takes long enough that the agent run
# aborts first ("plugin approval wait cancelled by run abort"). This stands in for the person
# being quick, so the grant lands inside the run's patience window.
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use 24 >/dev/null
export CTRLRUN_OPENCLAW_TOKEN=probe-token-local
TOK=gap-token
echo "[grant] watching for held approvals"
for i in $(seq 1 400); do
  ID=$(openclaw --profile gap approvals pending --json --token $TOK 2>/dev/null \
       | python3 -c "import json,sys
try: a=json.load(sys.stdin).get('approvals',[])
except Exception: a=[]
print(a[0]['id'] if a else '')" 2>/dev/null)
  if [ -n "$ID" ]; then
    echo "[grant] $(date +%H:%M:%S) granting $ID"
    openclaw --profile gap approvals resolve "$ID" allow-once --token $TOK 2>&1 | tail -1
  fi
  sleep 0.5
done
