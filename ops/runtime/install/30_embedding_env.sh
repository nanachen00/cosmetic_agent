#!/usr/bin/env bash
# BGE-M3 embedding (:8002) and optional reranker env.
# Reuses the image's ROCm torch env; torch is pinned so sentence-transformers cannot replace it.
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"

log "30. Embedding 環境 $EMBED_ENV"
PY="$EMBED_ENV/bin/python"
[[ -x "$PY" ]] || die "找不到 $PY；可改設 EMBED_ENV 指向有 ROCm torch 的環境"
PIN="$(mktemp)"
"$PY" -m pip list --format=freeze | grep -iE '^(torch|torchvision|pytorch-triton-rocm|triton)==' > "$PIN"
echo "鎖定："; cat "$PIN"
"$PY" -m pip install -c "$PIN" "sentence-transformers>=3.0,<5" "fastapi>=0.115,<0.116" "uvicorn>=0.30" requests
"$PY" - <<'PY'
import torch, sentence_transformers
print("torch", torch.__version__, "hip", torch.version.hip, "| sentence-transformers", sentence_transformers.__version__)
assert torch.version.hip, "torch lost its ROCm build"
PY
