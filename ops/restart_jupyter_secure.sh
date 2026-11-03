#!/usr/bin/env bash
set -euo pipefail

JUPYTER_BIN="/opt/conda/envs/py_3.10/bin/jupyter-lab"
CONFIG_FILE="/var/lib/jenkins/tomod/ops/jupyter_secure_config.py"
TOKEN_FILE="/var/lib/jenkins/.jupyter_lab_token"
COOKIE_SECRET_FILE="/var/lib/jenkins/.jupyter_cookie_secret_secure"
SECURE_LOG="/var/lib/jenkins/jupyter-secure.log"
ROLLBACK_LOG="/var/lib/jenkins/jupyter-rollback.log"

if [[ ! -x "$JUPYTER_BIN" || ! -r "$CONFIG_FILE" || ! -s "$TOKEN_FILE" || ! -s "$COOKIE_SECRET_FILE" ]]; then
  echo "preflight files missing" >&2
  exit 2
fi

chmod 600 "$TOKEN_FILE"
chmod 600 "$COOKIE_SECRET_FILE"

mapfile -t old_pids < <(
  pgrep -f '/jupyter-lab (.*--port=8889|.*--config=/var/lib/jenkins/tomod/ops/jupyter_secure_config.py)' || true
)
if [[ ${#old_pids[@]} -ne 1 ]]; then
  echo "expected one existing Jupyter process on port 8889" >&2
  exit 3
fi

old_pid="${old_pids[0]}"
kill "$old_pid"
for _ in {1..40}; do
  if ! kill -0 "$old_pid" 2>/dev/null; then
    break
  fi
  sleep 0.25
done

nohup "$JUPYTER_BIN" --config="$CONFIG_FILE" >>"$SECURE_LOG" 2>&1 </dev/null &
secure_pid=$!

for _ in {1..60}; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: token $(<"$TOKEN_FILE")" \
    http://127.0.0.1:8889/api/status 2>/dev/null || true)"
  if [[ "$status" == "200" ]]; then
    echo "secure Jupyter started as PID $secure_pid"
    exit 0
  fi
  if ! kill -0 "$secure_pid" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

kill "$secure_pid" 2>/dev/null || true
nohup "$JUPYTER_BIN" \
  --notebook-dir=/var/lib \
  --ip=0.0.0.0 \
  --port=8889 \
  --no-browser \
  --allow-root \
  --NotebookApp.token= \
  >>"$ROLLBACK_LOG" 2>&1 </dev/null &
echo "secure startup failed; restored the previous Jupyter command" >&2
exit 4
