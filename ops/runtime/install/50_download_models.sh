#!/usr/bin/env bash
# Download only explicitly selected, revision-pinned models.
# Examples:
#   ENABLE_TAIDE=1 REQUESTED_MODELS=taide-prod bash 50_download_models.sh
#   REQUESTED_MODELS=reranker bash 50_download_models.sh
#   REQUESTED_MODELS=photo-base bash 50_download_models.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../runtime.conf"
[[ "$ENABLE_TAIDE" =~ ^[01]$ ]] || die "ENABLE_TAIDE 必須是 0 或 1"
# Validate the entire request before verifying/downloading any model, so a
# forbidden later item cannot cause a partial earlier download.
REQUESTED_MODELS="${REQUESTED_MODELS:-embedding}"
IFS=',' read -r -a requested <<<"$REQUESTED_MODELS"
for key in "${requested[@]}"; do
  key="${key//[[:space:]]/}"
  case "$key" in
    taide-prod|taide-ab)
      [[ "$ENABLE_TAIDE" == "1" ]] || die "$key 需要明確指定 ENABLE_TAIDE=1"
      [[ -x "$VLLM_ENV/bin/python" ]] || die "缺少已選 TAIDE 模型所需的 vLLM Python；不修改或安裝舊環境"
      ;;
    embedding|reranker|photo-base)
      [[ -x "$EMBED_ENV/bin/python" ]] || die "缺少獨立 BGE Python：$EMBED_ENV/bin/python；不回退至 vLLM"
      [[ "$(cd "$EMBED_ENV" && pwd -P)" != "$(cd "$VLLM_ENV" 2>/dev/null && pwd -P || true)" ]] \
        || die "模型下載器不能使用舊 vLLM 共用環境作為 BGE Python"
      ;;
    "") ;;
    *) die "未知模型 key：$key（允許 taide-prod,taide-ab,embedding,reranker,photo-base）" ;;
  esac
done
"$SCRIPT_DIR/../verify-mi300x-host.sh"
"$SCRIPT_DIR/../verify-mi300x-mount.sh"

export HF_HOME HF_HUB_ENABLE_HF_TRANSFER=0

download() { # key repo revision target gated ignore_patterns
  local key="$1" repo="$2" revision="$3" target="$4" gated="$5" ignores="$6"
  local model_python="$EMBED_ENV/bin/python"
  [[ "$key" != taide-* ]] || model_python="$VLLM_ENV/bin/python"
  if [[ "$gated" == "1" && -z "${HF_TOKEN:-}" ]]; then
    die "$repo 是 gated 模型，需由已接受授權的帳號提供 HF_TOKEN"
  fi
  if "$model_python" "$SCRIPT_DIR/../verify-model-artifacts.py" --models "$key" >/dev/null 2>&1; then
    printf '[SKIP] %s 已通過 revision 與 SHA-256 驗證：%s\n' "$repo" "$target"
    return
  fi
  [[ ! -e "$target" || -d "$target" ]] || die "$target 不是資料夾"
  log "下載單一模型 $repo@$revision → $target"
  REPO="$repo" REVISION="$revision" TARGET="$target" IGNORE_PATTERNS="$ignores" \
    "$model_python" - <<'PY'
import hashlib
import json
import os
from pathlib import Path
from huggingface_hub import snapshot_download

target = Path(os.environ["TARGET"])
ignore_patterns = [item for item in os.environ.get("IGNORE_PATTERNS", "").split(",") if item]
snapshot_download(
    repo_id=os.environ["REPO"],
    revision=os.environ["REVISION"],
    local_dir=target,
    token=os.environ.get("HF_TOKEN") or None,
    allow_patterns=[
        "*.json", "*.safetensors", "*.bin", "*.model", "*.txt", "*.jinja",
        "tokenizer*", "sentence_bert_config.json", "modules.json", "1_Pooling/*",
    ],
    ignore_patterns=ignore_patterns or None,
    max_workers=4,
)
files = {}
for path in sorted(target.glob("*.safetensors")) + sorted(target.glob("pytorch_model*.bin")):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    files[path.name] = digest.hexdigest()
if not files:
    raise SystemExit("download completed without a recognized model weight file")
metadata = {
    "schema_version": 1,
    "repo_id": os.environ["REPO"],
    "revision": os.environ["REVISION"],
    "files": files,
}
(target / ".blink-artifact.json").write_text(
    json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
  "$model_python" "$SCRIPT_DIR/../verify-model-artifacts.py" --models "$key"
}

for key in "${requested[@]}"; do
  key="${key//[[:space:]]/}"
  case "$key" in
    taide-prod)
      download "$key" "$PROD_MODEL_REPO" "$PROD_MODEL_REVISION" "$PROD_MODEL_PATH" 1 ""
      ;;
    taide-ab)
      download "$key" "$AB_MODEL_REPO" "$AB_MODEL_REVISION" "$AB_MODEL_PATH" 1 ""
      ;;
    embedding)
      download "$key" "$EMBEDDING_MODEL_REPO" "$EMBEDDING_MODEL_REVISION" "$EMBEDDING_MODEL_PATH" 0 "onnx/*,*.onnx,imgs/*"
      ;;
    reranker)
      download "$key" "$RERANKER_MODEL_REPO" "$RERANKER_MODEL_REVISION" "$RERANKER_MODEL_PATH" 0 "onnx/*,*.onnx,imgs/*"
      ;;
    photo-base)
      download "$key" "$PHOTO_MODEL_REPO" "$PHOTO_MODEL_REVISION" "$PHOTO_MODEL_PATH" 0 ""
      ;;
    "") ;;
    *) die "未知模型 key：$key（允許 taide-prod,taide-ab,embedding,reranker,photo-base）" ;;
  esac
done
