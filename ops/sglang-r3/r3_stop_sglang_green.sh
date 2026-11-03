#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm-v051p2}"
PORT=8100
PIDFILE="$ROOT/ops/runtime/pids/sglang-green-r3-8100.pid"

listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

if [[ ! -f "$PIDFILE" ]]; then
  if listener "$PORT"; then
    echo "[REFUSE] :8100 is listening but R3 pidfile is missing."
    exit 2
  fi
  echo "[OK] Green is already down."
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

exe = str(proc.joinpath("exe").resolve())
cmd = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")

if not exe.startswith(env + "/"):
    raise SystemExit(f"[REFUSE] executable outside Green env: {exe}")
if "sglang" not in cmd or "8100" not in cmd:
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

print("[OK] validated SGLang process exited")
PY

rm -f "$PIDFILE"

if listener "$PORT"; then
  echo "[ERROR] :8100 still listening"
  exit 3
fi

echo "[OK] :8100 clear"
