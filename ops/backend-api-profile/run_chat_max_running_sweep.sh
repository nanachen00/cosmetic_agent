#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
PROFILE_ROOT="${PROFILE_ROOT:-$ROOT/ops/backend-api-profile}"
ENV_FILE="${ENV_FILE:-$ROOT/ops/runtime/env/backend-8010.env}"
RUNTIME_LOG="${RUNTIME_LOG:-$ROOT/ops/runtime/logs/backend-8010.log}"
PID_FILE="${PID_FILE:-$ROOT/ops/runtime/pids/backend-8010.pid}"
PROFILER="${PROFILER:-$PROFILE_ROOT/run_backend_api_latency_profile.py}"

# Default experiment set requested for the first admission sweep.
# Override, for example:
#   RUNNING_VALUES="4 8" bash run_chat_max_running_sweep.sh
RUNNING_VALUES="${RUNNING_VALUES:-4 8 16}"

FORMAL_SCENARIOS="${FORMAL_SCENARIOS:-product_search,skin_consult,auto_root}"
SCENARIO_COOLDOWN_SECONDS="${SCENARIO_COOLDOWN_SECONDS:-65}"
CASE_COOLDOWN_SECONDS="${CASE_COOLDOWN_SECONDS:-15}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-180}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
SWEEP_DIR="$PROFILE_ROOT/admission-sweep/$STAMP"
RESTART_DIR="$SWEEP_DIR/restarts"
BACKUP_DIR="$SWEEP_DIR/baseline"

mkdir -p "$SWEEP_DIR" "$RESTART_DIR" "$BACKUP_DIR"

exec > >(tee -a "$SWEEP_DIR/sweep-console.log") 2>&1

echo "============================================================"
echo "BLINK CHAT_MAX_RUNNING admission sweep"
echo "============================================================"
echo "ROOT=$ROOT"
echo "ENV_FILE=$ENV_FILE"
echo "PROFILER=$PROFILER"
echo "RUNNING_VALUES=$RUNNING_VALUES"
echo "SWEEP_DIR=$SWEEP_DIR"
echo

[[ -s "$ENV_FILE" ]] || {
  echo "[ERROR] Canonical backend-8010 env file missing: $ENV_FILE"
  exit 1
}
[[ -f "$PROFILER" ]] || {
  echo "[ERROR] Profiler missing: $PROFILER"
  exit 1
}
[[ -s "$ROOT/cosmetic-agent/api/api_server.py" ]] || {
  echo "[ERROR] api_server.py missing."
  exit 1
}

cp -a -- "$ENV_FILE" "$BACKUP_DIR/backend-8010.env.before"

# ------------------------------------------------------------------
# Verify current source still uses the environment variable, with
# code default 2. This script never edits api_server.py.
# ------------------------------------------------------------------
python3 - "$ROOT/cosmetic-agent/api/api_server.py" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
needle = 'max_running=int(os.getenv("CHAT_MAX_RUNNING", "2"))'
if needle not in text:
    raise SystemExit(
        "[ERROR] Expected CHAT_MAX_RUNNING env contract/default=2 not found "
        "in api_server.py; refusing experiment."
    )
print("[OK] api_server.py reads CHAT_MAX_RUNNING with code default 2.")
PY

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
get_env_value() {
  python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
values = []
for line in lines:
    m = re.match(r'^\s*(?:export\s+)?CHAT_MAX_RUNNING\s*=\s*(.*?)\s*$', line)
    if m:
        values.append(m.group(1).strip().strip('"').strip("'"))
if len(values) > 1:
    raise SystemExit("[ERROR] Duplicate CHAT_MAX_RUNNING entries in env file.")
print(values[0] if values else "<MISSING>")
PY
}

set_env_value() {
  local value="$1"
  python3 - "$ENV_FILE" "$value" <<'PY'
from pathlib import Path
import os
import re
import sys
import tempfile

path = Path(sys.argv[1])
value = sys.argv[2]

if not value.isdigit() or int(value) < 1:
    raise SystemExit("[ERROR] CHAT_MAX_RUNNING must be a positive integer.")

text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
pattern = re.compile(r'^\s*(?:export\s+)?CHAT_MAX_RUNNING\s*=.*$')
matches = [i for i, line in enumerate(lines) if pattern.match(line.rstrip("\n"))]

if len(matches) > 1:
    raise SystemExit("[ERROR] Duplicate CHAT_MAX_RUNNING entries; refusing edit.")

new_line = f"CHAT_MAX_RUNNING={value}\n"
if matches:
    lines[matches[0]] = new_line
else:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(new_line)

fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".new.", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(lines))
        f.flush()
        os.fsync(f.fileno())

    st = path.stat()
    os.chmod(tmp_name, st.st_mode)
    try:
        os.chown(tmp_name, st.st_uid, st.st_gid)
    except PermissionError:
        pass

    os.replace(tmp_name, path)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)

print(f"[PATCH] CHAT_MAX_RUNNING={value}")
PY
}

find_8010_pid() {
  timeout 2s ss -H -ltnp 'sport = :8010' 2>/dev/null \
    | grep -oP 'pid=\K[0-9]+' \
    | head -1 \
    || true
}

restart_8010() {
  local value="$1"
  local tag="$2"
  local out="$RESTART_DIR/$tag"
  mkdir -p "$out"

  local old_pid
  old_pid="$(find_8010_pid)"
  if [[ -z "$old_pid" ]]; then
    echo "[ERROR] :8010 has no listener; refusing to guess launch command."
    return 1
  fi

  local cwd
  cwd="$(readlink -f "/proc/$old_pid/cwd")"
  [[ "$cwd" == "$ROOT/cosmetic-agent" ]] || {
    echo "[ERROR] Unexpected :8010 cwd: $cwd"
    return 1
  }

  mapfile -d '' -t argv < "/proc/$old_pid/cmdline"
  if ((${#argv[@]} < 4)); then
    echo "[ERROR] Could not capture :8010 argv."
    return 1
  fi

  local joined
  printf -v joined '%q ' "${argv[@]}"
  {
    echo "requested_CHAT_MAX_RUNNING=$value"
    echo "old_pid=$old_pid"
    echo "cwd=$cwd"
    echo "argv=$joined"
    echo "env_file=$ENV_FILE"
    echo "timestamp_utc=$(date -u --iso-8601=seconds)"
  } > "$out/pre-restart.txt"

  if [[ "$joined" != *"uvicorn"* || "$joined" != *"api.api_server:app"* || "$joined" != *"--port 8010"* ]]; then
    echo "[ERROR] Unexpected :8010 argv; refusing SIGTERM."
    cat "$out/pre-restart.txt"
    return 1
  fi

  kill -TERM "$old_pid"

  for _ in $(seq 1 30); do
    if ! kill -0 "$old_pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  if kill -0 "$old_pid" 2>/dev/null; then
    echo "[ERROR] :8010 PID $old_pid did not exit in 30s; no SIGKILL sent."
    return 1
  fi

  if [[ -n "$(find_8010_pid)" ]]; then
    echo "[ERROR] :8010 was replaced externally before controlled restart."
    return 1
  fi

  (
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    cd "$cwd"

    nohup "${argv[@]}" >> "$RUNTIME_LOG" 2>&1 &
    echo $! > "$PID_FILE"
  )

  local new_pid
  new_pid="$(cat "$PID_FILE")"
  echo "new_pid=$new_pid" >> "$out/pre-restart.txt"

  local ready=0
  for _ in $(seq 1 90); do
    if curl -fsS --max-time 5 \
      http://127.0.0.1:8010/api/v1/live \
      > "$out/live.json" 2> "$out/live.err"
    then
      ready=1
      break
    fi
    sleep 1
  done

  if [[ "$ready" -ne 1 ]]; then
    echo "[ERROR] :8010 did not become live."
    tail -200 "$RUNTIME_LOG" > "$out/backend-8010-tail.log" || true
    return 1
  fi

  new_pid="$(find_8010_pid)"
  if [[ -z "$new_pid" ]]; then
    echo "[ERROR] :8010 live probe passed but listener PID lookup failed."
    return 1
  fi

  local effective
  effective="$(
    tr '\0' '\n' < "/proc/$new_pid/environ" \
      | sed -n 's/^CHAT_MAX_RUNNING=//p' \
      | head -1
  )"

  {
    echo "new_listener_pid=$new_pid"
    echo "effective_CHAT_MAX_RUNNING=${effective:-<MISSING>}"
    echo
    echo "===== LIVE ====="
    cat "$out/live.json"
    echo
    echo "===== HEALTH ====="
    curl -sS --max-time 10 \
      -w '\nHTTP=%{http_code}\n' \
      http://127.0.0.1:8010/api/v1/health || true
  } > "$out/post-restart.txt"

  if [[ "$effective" != "$value" ]]; then
    echo "[ERROR] New :8010 effective CHAT_MAX_RUNNING=$effective, expected $value"
    cat "$out/post-restart.txt"
    return 1
  fi

  echo "[OK] :8010 restarted with CHAT_MAX_RUNNING=$value"
}

vu_list_for_running() {
  local value="$1"
  case "$value" in
    1|2)  echo "1,2" ;;
    3|4)  echo "1,2,4,8" ;;
    5|6|7|8) echo "1,2,4,8,16" ;;
    *)
      # Current profiler intentionally stops at VU16 because guest-session
      # bootstrap is protected by a 20/600s per-IP production rate limit.
      echo "1,2,4,8,16"
      ;;
  esac
}

RESTORE_DONE=0

restore_two() {
  local reason="${1:-exit}"
  if [[ "$RESTORE_DONE" -eq 1 ]]; then
    return 0
  fi

  echo
  echo "============================================================"
  echo "RESTORE CHAT_MAX_RUNNING=2 ($reason)"
  echo "============================================================"

  set +e
  set_env_value 2
  restart_8010 2 "restore-2"
  local rc=$?
  set -e

  if [[ "$rc" -eq 0 ]]; then
    RESTORE_DONE=1
    echo "CHAT_MAX_RUNNING=2" > "$SWEEP_DIR/RESTORED_TO_2.txt"
    echo "[RESTORE] PASS"
    return 0
  fi

  echo "[RESTORE] FAILED — manual attention required."
  echo "CHAT_MAX_RUNNING=2 was written to $ENV_FILE, but restart verification failed." \
    > "$SWEEP_DIR/RESTORE_FAILED.txt"
  return 1
}

on_exit() {
  local rc=$?
  if [[ "$RESTORE_DONE" -ne 1 ]]; then
    restore_two "trap after rc=$rc" || true
  fi
  exit "$rc"
}
trap on_exit EXIT INT TERM

# ------------------------------------------------------------------
# Baseline contract.
# Missing env entry is accepted only because source default is exactly 2.
# An explicit non-2 starting value is refused.
# ------------------------------------------------------------------
ORIGINAL_ENV_VALUE="$(get_env_value)"
echo "original_env_CHAT_MAX_RUNNING=$ORIGINAL_ENV_VALUE" \
  | tee "$BACKUP_DIR/original-effective.txt"

if [[ "$ORIGINAL_ENV_VALUE" != "<MISSING>" && "$ORIGINAL_ENV_VALUE" != "2" ]]; then
  echo "[ERROR] Expected original CHAT_MAX_RUNNING to be 2 or code-default/missing."
  exit 1
fi

cat > "$SWEEP_DIR/experiment.txt" <<EOF
started_utc=$(date -u --iso-8601=seconds)
original_env_CHAT_MAX_RUNNING=$ORIGINAL_ENV_VALUE
source_default_CHAT_MAX_RUNNING=2
running_values=$RUNNING_VALUES
formal_scenarios=$FORMAL_SCENARIOS
scenario_cooldown_seconds=$SCENARIO_COOLDOWN_SECONDS
case_cooldown_seconds=$CASE_COOLDOWN_SECONDS
policy=modify backend-8010 env only; restart :8010 only; restore explicit 2
EOF

# ------------------------------------------------------------------
# Experiment cases.
# ------------------------------------------------------------------
for value in $RUNNING_VALUES; do
  case "$value" in
    ''|*[!0-9]*)
      echo "[ERROR] Invalid RUNNING_VALUES item: $value"
      exit 1
      ;;
  esac

  if (( value < 1 )); then
    echo "[ERROR] CHAT_MAX_RUNNING must be >=1."
    exit 1
  fi

  case_dir="$SWEEP_DIR/max-running-$value"
  mkdir -p "$case_dir"

  echo
  echo "############################################################"
  echo "CASE CHAT_MAX_RUNNING=$value"
  echo "############################################################"

  set_env_value "$value"
  cp -a -- "$ENV_FILE" "$case_dir/backend-8010.env"

  restart_8010 "$value" "max-running-$value"

  echo
  echo "===== SMOKE max_running=$value ====="
  python3 "$PROFILER" smoke \
    --ops-dir "$case_dir/smoke" \
    --timeout-seconds "$REQUEST_TIMEOUT_SECONDS" \
    2>&1 | tee "$case_dir/smoke-console.log"

  vu_list="$(vu_list_for_running "$value")"
  echo "$vu_list" > "$case_dir/vu-list.txt"

  echo
  echo "===== FORMAL max_running=$value VU=$vu_list ====="
  python3 "$PROFILER" formal \
    --ops-dir "$case_dir/formal" \
    --vu-list "$vu_list" \
    --formal-scenarios "$FORMAL_SCENARIOS" \
    --cooldown-seconds "$SCENARIO_COOLDOWN_SECONDS" \
    --timeout-seconds "$REQUEST_TIMEOUT_SECONDS" \
    2>&1 | tee "$case_dir/formal-console.log"

  latest_formal="$(
    find "$case_dir/formal/runs" -mindepth 1 -maxdepth 1 -type d \
      | sort \
      | tail -1
  )"

  if [[ -n "$latest_formal" && -f "$latest_formal/summary.csv" ]]; then
    cp "$latest_formal/summary.csv" "$case_dir/summary.csv"
    cp "$latest_formal/final-status.json" "$case_dir/final-status.json"
  else
    echo "[ERROR] Formal result directory not found for max_running=$value"
    exit 1
  fi

  echo "[CASE PASS] CHAT_MAX_RUNNING=$value"

  if [[ "$CASE_COOLDOWN_SECONDS" -gt 0 ]]; then
    echo "Case cooldown: ${CASE_COOLDOWN_SECONDS}s"
    sleep "$CASE_COOLDOWN_SECONDS"
  fi
done

# ------------------------------------------------------------------
# Aggregate per-case summary before restoration.
# ------------------------------------------------------------------
python3 - "$SWEEP_DIR" <<'PY'
from pathlib import Path
import csv
import sys

root = Path(sys.argv[1])
rows = []

for case in sorted(root.glob("max-running-*"), key=lambda p: int(p.name.rsplit("-", 1)[1])):
    value = int(case.name.rsplit("-", 1)[1])
    summary = case / "summary.csv"
    if not summary.exists():
        continue
    with summary.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {"chat_max_running": value, **row}
            rows.append(row)

out = root / "admission-sweep-summary.csv"
if rows:
    fields = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
else:
    out.write_text("", encoding="utf-8")
PY

# Explicit requested final state.
restore_two "normal completion"
trap - EXIT INT TERM

echo
echo "============================================================"
echo "ADMISSION SWEEP COMPLETE"
echo "============================================================"
echo "Results: $SWEEP_DIR"
echo "Final CHAT_MAX_RUNNING=2"
echo "Summary: $SWEEP_DIR/admission-sweep-summary.csv"
