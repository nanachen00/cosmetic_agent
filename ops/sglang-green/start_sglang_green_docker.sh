#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"; MODEL="${SGLANG_MODEL_PATH:-$ROOT/taide-12b}"; IMAGE="${SGLANG_ROCM_IMAGE:-}"; NAME=blink-sglang-green-8100
[[ -n "$IMAGE" ]] || { echo '[ERROR] Set SGLANG_ROCM_IMAGE explicitly'; exit 2; }
command -v docker >/dev/null 2>&1 || { echo '[ERROR] docker not found'; exit 2; }
! ss -H -ltn 'sport = :8100' 2>/dev/null | grep -q . || { echo '[ERROR] :8100 already listening'; exit 2; }
ss -H -ltn 'sport = :8000' 2>/dev/null | grep -q . || { echo '[ERROR] Blue :8000 must remain up'; exit 2; }
! docker inspect "$NAME" >/dev/null 2>&1 || { echo "[ERROR] container $NAME already exists"; exit 2; }
echo "Image: $IMAGE"; echo 'No image pull will be performed.'
docker run -d --name "$NAME" --network host --device=/dev/kfd --device=/dev/dri --group-add video --ipc=host --shm-size 32g -v "$MODEL:/models/taide-12b:ro" "$IMAGE" sglang serve --model-path /models/taide-12b --served-model-name taide-12b --host 127.0.0.1 --port 8100 --dtype bfloat16 --context-length "${SGLANG_CONTEXT_LENGTH:-32768}" --mem-fraction-static "${SGLANG_MEM_FRACTION_STATIC:-0.30}" --max-running-requests "${SGLANG_MAX_RUNNING_REQUESTS:-8}" --enable-metrics --sampling-defaults openai --disable-cuda-graph
echo "[STARTED] $NAME"; echo "Use: docker logs -f $NAME"
