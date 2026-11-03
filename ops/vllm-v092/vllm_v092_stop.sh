#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
PIDFILE="$ROOT/ops/runtime/pids/vllm-v092-green-8200.pid"

listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

if [[ ! -f "$PIDFILE" ]]; then
  if listener 8200; then
    echo "[REFUSE] :8200 is listening but vLLM pidfile is missing."
    exit 2
  fi
  echo "[OK] vLLM candidate already down."
  exit 0
fi

PID="$(cat "$PIDFILE")"

python3 - "$PID" "$GREEN_ENV" <<'PY'
import os, select, signal, sys
from pathlib import Path

pid = int(sys.argv[1])
env = str(Path(sys.argv[2]).resolve())
proc = Path(f"/proc/{pid}")
if not proc.exists():
    print("[INFO] process already exited")
    raise SystemExit(0)

exe = str((proc / "exe").resolve())
cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
if not exe.startswith(env + "/"):
    raise SystemExit(f"[REFUSE] executable outside Green env: {exe}")
if "vllm.entrypoints.openai.api_server" not in cmd or "8200" not in cmd:
    raise SystemExit(f"[REFUSE] process identity mismatch: {cmd!r}")

if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
    raise SystemExit("[REFUSE] pidfd APIs unavailable")

fd = os.pidfd_open(pid, 0)
try:
    signal.pidfd_send_signal(fd, signal.SIGTERM)
    if not select.select([fd], [], [], 90)[0]:
        raise SystemExit("[ERROR] process did not exit within 90s; no SIGKILL sent")
finally:
    os.close(fd)

print("[OK] validated vLLM process exited")
PY

rm -f "$PIDFILE"
if listener 8200; then
  echo "[ERROR] :8200 still listening"
  exit 3
fi
echo "[OK] :8200 clear"
