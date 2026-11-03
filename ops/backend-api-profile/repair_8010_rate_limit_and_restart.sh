#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
TARGET="$ROOT/cosmetic-agent/api/api_server.py"
PROFILE_DIR="$ROOT/ops/backend-api-profile"
ENV_FILE="$ROOT/ops/runtime/env/backend-8010.env"
LOG_FILE="$ROOT/ops/runtime/logs/backend-8010.log"
PID_FILE="$ROOT/ops/runtime/pids/backend-8010.pid"
PYTHON_BIN="/opt/conda/envs/rocm_env/bin/python"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$PROFILE_DIR/repair/$STAMP"

EXPECTED_SHA="32b56de8f561eaa04931f6ee0a215ac11723b3acac4535df5f923e76a3f46302"

mkdir -p "$RUN_DIR"

sha() { sha256sum "$1" | awk '{print $1}'; }

echo "============================================================"
echo "BLINK :8010 pre-existing rate-limit middleware repair"
echo "============================================================"
echo "TARGET=$TARGET"
echo "RUN_DIR=$RUN_DIR"

[[ -f "$TARGET" ]] || { echo "[ERROR] Missing $TARGET"; exit 1; }
[[ -s "$ENV_FILE" ]] || { echo "[ERROR] Missing canonical env $ENV_FILE"; exit 1; }
[[ -x "$PYTHON_BIN" ]] || { echo "[ERROR] Missing $PYTHON_BIN"; exit 1; }

CURRENT_SHA="$(sha "$TARGET")"
echo "current_sha=$CURRENT_SHA"

if [[ "$CURRENT_SHA" != "$EXPECTED_SHA" ]]; then
    echo "[ERROR] api_server.py differs from the reviewed profiling-patched source."
    echo "        Refusing to modify unknown source."
    exit 1
fi

cp -a -- "$TARGET" "$RUN_DIR/api_server.py.before"

python3 - "$TARGET" "$RUN_DIR/api_server.py.new" <<'PY'
from pathlib import Path
import ast
import sys

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding="utf-8")

needle = (
    '    if content_length > _MAX_API_BODY_BYTES:\n'
    '            return JSONResponse(status_code=413, content={"detail": "Request body too large"})\n'
    '\n'
    '\n'
    '    if rule is not None:\n'
)

replacement = (
    '    if content_length > _MAX_API_BODY_BYTES:\n'
    '            return JSONResponse(status_code=413, content={"detail": "Request body too large"})\n'
    '\n'
    '    rule = _API_RATE_LIMITS.get((request.method, request.url.path))\n'
    '\n'
    '    if rule is not None:\n'
)

if needle not in text:
    raise SystemExit(
        "[ERROR] Expected broken middleware block not found; refusing to guess."
    )

if "rule = _API_RATE_LIMITS.get((request.method, request.url.path))" in text:
    raise SystemExit("[ERROR] Rate-limit lookup already present; nothing to repair.")

patched = text.replace(needle, replacement, 1)
ast.parse(patched, filename=str(src))
dst.write_text(patched, encoding="utf-8")
PY

diff -u -- "$TARGET" "$RUN_DIR/api_server.py.new" > "$RUN_DIR/api_server.py.diff" || true

echo
echo "===== REVIEWED DIFF ====="
cat "$RUN_DIR/api_server.py.diff"

ADDED_NONHEADER="$(
    grep '^+' "$RUN_DIR/api_server.py.diff"       | grep -v '^+++'       | sed 's/^+//'       | grep -v '^[[:space:]]*$'       || true
)"
EXPECTED_LINE='    rule = _API_RATE_LIMITS.get((request.method, request.url.path))'

if [[ "$ADDED_NONHEADER" != "$EXPECTED_LINE" ]]; then
    echo "[ERROR] Diff contains an unexpected nonblank addition."
    exit 1
fi

REMOVED_NONHEADER="$(
    grep '^-' "$RUN_DIR/api_server.py.diff"       | grep -v '^---'       | sed 's/^-//'       | grep -v '^[[:space:]]*$'       || true
)"
if [[ -n "$REMOVED_NONHEADER" ]]; then
    echo "[ERROR] Diff removes nonblank source lines; refusing repair."
    exit 1
fi

tmp="${TARGET}.repair.$$"
cp -- "$RUN_DIR/api_server.py.new" "$tmp"
chown --reference="$TARGET" "$tmp"
chmod --reference="$TARGET" "$tmp"
mv -fT -- "$tmp" "$TARGET"

NEW_SHA="$(sha "$TARGET")"
echo "new_sha=$NEW_SHA" | tee "$RUN_DIR/new-sha.txt"

echo
echo "============================================================"
echo "RESTART :8010 ONLY WITH CANONICAL RUNTIME ENV"
echo "============================================================"

LISTENER_PID="$(
    ss -H -ltnp 2>/dev/null       | grep -E ':8010([[:space:]]|$)'       | grep -oP 'pid=\K[0-9]+'       | head -1       || true
)"

if [[ -z "$LISTENER_PID" ]]; then
    echo "[ERROR] No current :8010 listener. Source is repaired, but automatic restart aborted."
    exit 1
fi

CWD="$(readlink -f "/proc/$LISTENER_PID/cwd")"
CMD="$(tr '\0' ' ' < "/proc/$LISTENER_PID/cmdline")"

echo "old_pid=$LISTENER_PID"
echo "old_cwd=$CWD"
echo "old_cmd=$CMD"

if [[ "$CWD" != "$ROOT/cosmetic-agent" ]]; then
    echo "[ERROR] :8010 cwd is unexpected; refusing to signal."
    exit 1
fi
if [[ "$CMD" != *"-m uvicorn api.api_server:app"* || "$CMD" != *"--port 8010"* ]]; then
    echo "[ERROR] :8010 command is unexpected; refusing to signal."
    exit 1
fi

kill -TERM "$LISTENER_PID"

for _ in $(seq 1 30); do
    if ! kill -0 "$LISTENER_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done

if kill -0 "$LISTENER_PID" 2>/dev/null; then
    echo "[ERROR] Old :8010 process did not exit within 30 seconds; no SIGKILL sent."
    exit 1
fi

if ss -H -ltn 2>/dev/null | grep -qE ':8010([[:space:]]|$)'; then
    echo "[ERROR] :8010 still occupied after expected process exited."
    exit 1
fi

(
    set -a
    source "$ENV_FILE"
    set +a

    cd "$ROOT/cosmetic-agent"

    nohup       "$PYTHON_BIN"       -m uvicorn       api.api_server:app       --host 0.0.0.0       --port 8010       >> "$LOG_FILE" 2>&1 &

    echo $! > "$PID_FILE"
)

NEW_PID="$(cat "$PID_FILE")"
echo "new_pid=$NEW_PID"

READY=0
for _ in $(seq 1 90); do
    if curl -fsS --max-time 5         http://127.0.0.1:8010/api/v1/live         > "$RUN_DIR/live.json" 2> "$RUN_DIR/live.err"
    then
        READY=1
        break
    fi
    sleep 1
done

if [[ "$READY" -ne 1 ]]; then
    echo "[ERROR] :8010 did not pass /api/v1/live within 90 seconds."
    tail -200 "$LOG_FILE" > "$RUN_DIR/backend-8010-tail.log" || true
    cat "$RUN_DIR/backend-8010-tail.log"
    exit 1
fi

echo
echo "===== /api/v1/live ====="
cat "$RUN_DIR/live.json"
echo

for path in /api/v1/health /api/v1/ready; do
    safe_name="$(echo "$path" | tr '/' '_' | sed 's/^_//')"
    code="$(
      curl -sS --max-time 10         -o "$RUN_DIR/${safe_name}.json"         -w '%{http_code}'         "http://127.0.0.1:8010$path"         || true
    )"
    echo "$path HTTP=$code"
    cat "$RUN_DIR/${safe_name}.json" 2>/dev/null || true
    echo
done

tail -200 "$LOG_FILE" > "$RUN_DIR/backend-8010-tail.log" || true

echo
echo "============================================================"
echo "REPAIR COMPLETE"
echo "============================================================"
echo "Only api_server.py rate-limit rule lookup was restored."
echo "Only :8010 was restarted."
echo "No provider/model/Agent/profile/tracing logic was changed."
echo "Artifacts: $RUN_DIR"
