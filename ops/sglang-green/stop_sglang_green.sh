#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"; PORT="${SGLANG_PORT:-8100}"; MODEL="${SGLANG_MODEL_PATH:-$ROOT/taide-12b}"; PIDFILE="$ROOT/ops/runtime/pids/sglang-green-${PORT}.pid"
[[ "$PORT" == 8100 ]] || { echo '[ERROR] refusing non-Green port'; exit 2; }
if [[ ! -f "$PIDFILE" ]]; then
  if ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .; then echo '[REFUSE] :8100 listening but pidfile missing'; exit 3; fi
  echo '[OK] no pidfile and :8100 not listening'; exit 0
fi
PID="$(cat "$PIDFILE")"
python3 - "$PID" "$PORT" "$MODEL" <<'PY'
import os,select,signal,sys
from pathlib import Path
pid=int(sys.argv[1]); port=sys.argv[2]; model=str(Path(sys.argv[3]).resolve()); proc=Path(f'/proc/{pid}')
if not proc.exists(): print('[INFO] pid already exited'); raise SystemExit(0)
cmd=proc.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
missing=[x for x in ('sglang',port,model) if x not in cmd]
if missing: raise SystemExit(f'[REFUSE] identity mismatch missing={missing}; cmd={cmd!r}')
if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'): raise SystemExit('[REFUSE] pidfd unavailable')
fd=os.pidfd_open(pid,0)
try:
    signal.pidfd_send_signal(fd,signal.SIGTERM)
    if not select.select([fd],[],[],60)[0]: raise SystemExit('[ERROR] no exit within 60s; no SIGKILL sent')
finally: os.close(fd)
print('[OK] validated Green process exited')
PY
rm -f "$PIDFILE"
! ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q . || { echo '[ERROR] :8100 still listening'; exit 4; }
echo '[OK] :8100 clear'
