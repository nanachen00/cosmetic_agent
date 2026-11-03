#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="${PROJECT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
[[ $# -ge 1 ]] || { echo "Usage: $0 {gate1|gate2|all} [--run-id ID] [Gate2 acknowledgements]" >&2; exit 2; }
CMD="$1"; shift
RUN_ID=""; REST=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2;;
    *) REST+=("$1"); shift;;
  esac
done
if [[ -z "$RUN_ID" ]]; then RUN_ID="$(date -u '+%Y%m%d-%H%M%S')-$$"; fi
mkdir -p "$ROOT/.state"
printf '%s\n' "$RUN_ID" > "$ROOT/.state/last_run_id"
echo "RUN_ID=$RUN_ID"
case "$CMD" in
  gate1)
    exec "$ROOT/gate1/run_gate1.sh" --run-id "$RUN_ID"
    ;;
  gate2)
    exec "$ROOT/gate2/run_gate2.sh" --run-id "$RUN_ID" "${REST[@]}"
    ;;
  all)
    "$ROOT/gate1/run_gate1.sh" --run-id "$RUN_ID"
    exec "$ROOT/gate2/run_gate2.sh" --run-id "$RUN_ID" "${REST[@]}"
    ;;
  *) echo "[ERROR] unknown command: $CMD" >&2; exit 2;;
esac
