#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="${PROJECT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
PROD="${PRODUCTION_ROOT:-/var/lib/jenkins/tomod/cosmetic-agent}"
RUN_ID="${RUN_ID:-}"
while [[ $# -gt 0 ]]; do case "$1" in --run-id) RUN_ID="$2"; shift 2;; *) echo "[ERROR] unknown arg: $1" >&2; exit 2;; esac; done
[[ -n "$RUN_ID" ]] || { echo "[ERROR] --run-id required" >&2; exit 2; }
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT="$ROOT/results/$RUN_ID/gate1"; PY="$ROOT/.runtime/kev-py312/bin/python"; PORT=8230
mkdir -p "$OUT" "$ROOT/.state/$RUN_ID/gate1" "$ROOT/.cache/tmp" "$ROOT/.cache/huggingface" "$ROOT/.cache/xdg"
[[ ! -e "$OUT/GATE_STATUS.json" ]] || { echo "[ERROR] Gate1 already finalized for run $RUN_ID" >&2; exit 4; }
[[ -x "$PY" ]] || { echo "[ERROR] runtime missing: $PY" >&2; exit 3; }
export PYTHONPATH="$ROOT/vendor/kev${PYTHONPATH:+:$PYTHONPATH}" HF_HOME="$ROOT/.cache/huggingface" XDG_CACHE_HOME="$ROOT/.cache/xdg" TMPDIR="$ROOT/.cache/tmp" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 KEV_DTYPE=bf16 KEV_FUSED=0 KEV_CUDA_GRAPHS=0 TOKENIZERS_PARALLELISM=false
PREFLIGHT_RC=99 PARSER_RC=99 SMOKE_RC=99 QUAL_RC=99 CLEANUP_PASS=false PID="" TICKS=""
port_free(){ "$PY" - "$1" <<'PY'
import socket,sys
s=socket.socket();s.settimeout(.2);used=s.connect_ex(('127.0.0.1',int(sys.argv[1])))==0;s.close();raise SystemExit(1 if used else 0)
PY
}
cleanup(){
  set +e
  same=false; stopped=true; signal=false
  if [[ -n "${PID:-}" && -r "/proc/$PID/stat" ]]; then
    cur="$(awk '{print $22}' "/proc/$PID/stat" 2>/dev/null)"
    if [[ "$cur" == "$TICKS" ]]; then same=true; kill -TERM "$PID" 2>/dev/null && signal=true; for _ in $(seq 1 150); do kill -0 "$PID" 2>/dev/null || break; sleep .1; done; if kill -0 "$PID" 2>/dev/null; then cur2="$(awk '{print $22}' "/proc/$PID/stat" 2>/dev/null)"; [[ "$cur2" == "$TICKS" ]] && kill -KILL "$PID" 2>/dev/null; fi; wait "$PID" 2>/dev/null || true; fi
  fi
  port_free "$PORT"; prc=$?; [[ $prc -eq 0 ]] || stopped=false
  if [[ -z "${PID:-}" ]]; then same=true; signal=true; fi
  [[ "$stopped" == true && "$same" == true && "$signal" == true ]] && CLEANUP_PASS=true || CLEANUP_PASS=false
  printf '{"same_process":%s,"signal_sent":%s,"stopped":%s,"port_released":%s}\n' "$same" "$signal" "$stopped" "$([[ $prc -eq 0 ]] && echo true || echo false)" > "$OUT/cleanup.json"
  PID=""
  set -e
}
trap cleanup EXIT INT TERM
set +e
"$PY" "$HERE/preflight.py" --project-root "$ROOT" --production-root "$PROD" --out "$OUT" >"$OUT/preflight.stdout.log" 2>"$OUT/preflight.stderr.log"; PREFLIGHT_RC=$?
set -e
if [[ $PREFLIGHT_RC -eq 0 ]]; then
  set +e
  "$PY" "$HERE/qualification.py" --out "$OUT/parser-preflight" --parser "$HERE/deterministic_parser.py" --intent-source "$PROD/tools/agent/intent_parser.py" --n-per-family 320 --seed 20260927 --parser-only >"$OUT/parser-preflight.log" 2>&1; PARSER_RC=$?
  set -e
fi
if [[ $PREFLIGHT_RC -eq 0 && $PARSER_RC -eq 0 ]]; then
  port_free "$PORT" || { echo "[ERROR] owned Kev port $PORT already in use" >&2; exit 5; }
  ADAPTER="$ROOT/.models/kev-0.8b-9a45d25eb2ab"
  "$PY" -m kev.serve --run "$ADAPTER" --host 127.0.0.1 --port "$PORT" >"$ROOT/.state/$RUN_ID/gate1/kev-service.log" 2>&1 & PID=$!
  TICKS="$(awk '{print $22}' "/proc/$PID/stat")"
  ready=0
  for _ in $(seq 1 240); do
    kill -0 "$PID" 2>/dev/null || break
    if "$PY" - "$PORT" "$OUT/models.json" <<'PY'
import json,sys,urllib.request
try:
 r=urllib.request.urlopen(f'http://127.0.0.1:{sys.argv[1]}/v1/models',timeout=1); open(sys.argv[2],'wb').write(r.read()); raise SystemExit(0 if r.status==200 else 1)
except Exception: raise SystemExit(1)
PY
    then ready=1; break; fi
    sleep 1
  done
  if [[ $ready -eq 1 ]]; then
    set +e
    "$PY" "$HERE/direct_smoke.py" --base-url "http://127.0.0.1:$PORT" --out "$OUT/direct-smoke.json" >"$OUT/direct-smoke.log" 2>&1; SMOKE_RC=$?
    "$PY" "$HERE/qualification.py" --base-url "http://127.0.0.1:$PORT" --out "$OUT/qualification" --parser "$HERE/deterministic_parser.py" --intent-source "$PROD/tools/agent/intent_parser.py" --n-per-family 320 --seed 20260927 >"$OUT/qualification.log" 2>&1; QUAL_RC=$?
    set -e
  else
    echo "[ERROR] Kev readiness failed" > "$OUT/kev-readiness-error.txt"
  fi
fi
cleanup
trap - EXIT INT TERM
set +e
"$PY" "$HERE/finalize.py" --out "$OUT" --production-root "$PROD" --preflight-rc "$PREFLIGHT_RC" --parser-rc "$PARSER_RC" --smoke-rc "$SMOKE_RC" --qualification-rc "$QUAL_RC" --cleanup-pass "$CLEANUP_PASS"
RC=$?
set -e
exit "$RC"
