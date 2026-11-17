#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

case "${BLINK_RUNTIME_ROLE:-}" in
  mac)
    runtime_dir="${MAC_RUNTIME_DIR:-${TMPDIR:-/tmp}/blink-cosmetic-agent}"
    pid_dir="$runtime_dir/pids"
    names=(frontend backend router)
    ;;
  mi300x)
    source "$SCRIPT_DIR/runtime.conf"
    verify_mi300x_control_python
    "$SCRIPT_DIR/verify-mi300x-mount.sh" --check-only
    pid_dir="$PIDDIR"
    if [[ -f /mnt/goodagents/outputs/runtime/pids/photo-model.identity.json ]]; then
      "$MI300X_CONTROL_PYTHON" "$MI300X_PHOTO_CONTROLLER" stop
    fi
    "$MI300X_CONTROL_PYTHON" "$MI300X_BGE_CONTROLLER" stop --instance bge-model
    # Legacy numeric PID files do not authorize signals after PID/container reuse.
    # TAIDE retirement needs its fresh reviewed plan; reranker needs identity review.
    for legacy_name in reranker embedding gateway-8000 vllm-ab vllm-prod; do
      if [[ -f "$pid_dir/$legacy_name.pid" ]]; then
        printf '[SKIP] legacy %s PID file retained; no signal sent without fresh identity review\n' "$legacy_name"
      fi
    done
    exit 0
    ;;
  *)
    echo '[ERROR] 設定 BLINK_RUNTIME_ROLE=mac 或 BLINK_RUNTIME_ROLE=mi300x' >&2
    exit 2
    ;;
esac

for name in "${names[@]}"; do
  pid_file="$pid_dir/$name.pid"
  [[ -f "$pid_file" ]] || continue
  pid="$(<"$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || { echo "[WARN] invalid pid file $pid_file"; continue; }
  if kill -0 "$pid" 2>/dev/null; then
    if [[ "${BLINK_RUNTIME_ROLE:-}" == mac ]]; then
      "$PROJECT_ROOT/cosmetic-agent/.venv/bin/python" - "$pid_dir/$name.identity.json" "$pid" "$name" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
path, pid, name = sys.argv[1:]
if not Path(path).is_file():
    raise SystemExit('[ERROR] Missing service identity; refusing to stop an unverified PID')
identity = json.loads(Path(path).read_text())
actual = subprocess.check_output(['ps','-p',pid,'-o','lstart='],text=True,env={**os.environ,'LC_ALL':'C'}).strip()
if identity != {'pid':int(pid), 'service':name, 'start':actual}:
    raise SystemExit('[ERROR] Service PID identity changed; refusing to signal it')
PY
    fi
    kill "$pid"
    for _ in {1..30}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "[WARN] $name PID=$pid did not stop after 30 seconds; left running" >&2
      continue
    fi
    echo "[STOP] $name PID=$pid"
  fi
  rm -f "$pid_file"
done
