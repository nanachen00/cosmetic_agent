#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="${PROJECT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
RESULTS_OUT="${RESULTS_OUT:-/var/lib/jenkins/tomod/ops/results}"
RUN_ID=""
while [[ $# -gt 0 ]]; do case "$1" in --run-id) RUN_ID="$2";shift 2;;*) echo "[ERROR] unknown arg: $1" >&2;exit 2;;esac; done
[[ -n "$RUN_ID" ]] || RUN_ID="$(cat "$ROOT/.state/last_run_id" 2>/dev/null || true)"
[[ -n "$RUN_ID" && -d "$ROOT/results/$RUN_ID" ]] || { echo "[ERROR] run result not found" >&2; exit 2; }
mkdir -p "$RESULTS_OUT"
STAMP="$(date '+%Y%m%d-%H%M%S')"; STAGE="$RESULTS_OUT/.gate12-pack-$STAMP-$$"; OUT="$RESULTS_OUT/blink-intent-gate12-$RUN_ID-$STAMP.tar.gz"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/run"
cp -a "$ROOT/results/$RUN_ID/." "$STAGE/run/"
# Never return credentials, state, raw service logs, raw process env, or model artifacts.
find "$STAGE" -type d \( -name service-logs -o -name raw-logs -o -name .private -o -name .state \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type f \( -name '*.log' -o -name '.env' -o -name '.env.*' -o -name 'accounts.json' -o -name 'cookies.json' -o -name 'credentials.json' -o -name 'backend-base-env.json' -o -name 'process-env.json' -o -name '*.safetensors' -o -name '*.pt' -o -name '*.pth' -o -name '*.onnx' -o -name '*.engine' \) -delete
cat > "$STAGE/PACKAGE_INFO.txt" <<EOF
BLINK Intent Parse Router Gate1-Gate2 result package
run_id=$RUN_ID
source=$ROOT/results/$RUN_ID
raw_service_logs=false
credentials=false
model_weights=false
EOF
tar -czf "$OUT" -C "$STAGE" run PACKAGE_INFO.txt
echo "UPLOAD THIS FILE:"
echo "  $OUT"
