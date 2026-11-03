#!/usr/bin/env bash
# Model weights. TAIDE repos are gated: HF_TOKEN must belong to an account that accepted
# the TAIDE license. The token is read from the environment only and never written to disk.
#
#   export HF_TOKEN=hf_xxx            # paste in the terminal, do not put in scripts
#   bash 50_download_models.sh        # prod + A/B TAIDE + BGE-M3 + reranker
#   DOWNLOAD_AB=0 bash 50_download_models.sh
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"

PY="$VLLM_ENV/bin/python"
[[ -x "$PY" ]] || die "先執行 40_vllm_env.sh"
mkdir -p "$MODEL_DIR" "$HF_HOME"
export HF_HOME HF_HUB_ENABLE_HF_TRANSFER=0

download() {  # repo target_dir needs_token
  local repo="$1" target="$2" gated="$3"
  if [[ -f "$target/config.json" ]] && ls "$target"/*.safetensors >/dev/null 2>&1; then
    echo "[SKIP] $repo 已在 $target"; return
  fi
  if [[ "$gated" == "1" && -z "${HF_TOKEN:-}" ]]; then
    die "$repo 是 gated 模型，請先 export HF_TOKEN"
  fi
  log "下載 $repo → $target"
  REPO="$repo" TARGET="$target" "$PY" - <<'PY'
import os
from huggingface_hub import snapshot_download
path = snapshot_download(
    repo_id=os.environ["REPO"],
    local_dir=os.environ["TARGET"],
    token=os.environ.get("HF_TOKEN") or None,
    allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt", "*.jinja", "tokenizer*"],
    max_workers=8,
)
print("done:", path)
PY
}

download "$PROD_MODEL_REPO" "$PROD_MODEL_PATH" "${PROD_MODEL_GATED:-1}"
[[ "${DOWNLOAD_AB:-1}" == "1" ]] && download "$AB_MODEL_REPO" "$AB_MODEL_PATH" "${AB_MODEL_GATED:-1}"

# Keep the historical path used by old scripts.
ln -sfn "$PROD_MODEL_PATH" "$ROOT/taide-12b"

log "下載 BGE-M3 embedding 與 reranker（寫入 HF cache）"
"$PY" - <<'PY'
from huggingface_hub import snapshot_download
for repo in ("BAAI/bge-m3", "BAAI/bge-reranker-v2-m3"):
    print("done:", snapshot_download(repo_id=repo, ignore_patterns=["onnx/*", "*.onnx", "imgs/*"]))
PY
du -sh "$MODEL_DIR"/* 2>/dev/null || true
