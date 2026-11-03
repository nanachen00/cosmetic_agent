#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# ============================================================
# BLINK Parser + Kev 0.8B collector
#
# Default:
#   code + configs + model metadata only
#
# Full:
#   ./collect_parser_kev.sh --with-model
#
# Output:
#   /var/lib/jenkins/tomod/ops/parser-kev-export/
# ============================================================

OPS="/var/lib/jenkins/tomod/ops"

ROOT="$OPS/blink-root-intent-system-one-benchmark"
ASSETS="$OPS/blink-root-intent-system-one-assets"

OUT_ROOT="$OPS/parser-kev-export"
STAMP="$(date '+%Y%m%d-%H%M%S')"

WITH_MODEL=0

case "${1:-}" in
  "")
    ;;
  --with-model)
    WITH_MODEL=1
    ;;
  *)
    echo "Usage:"
    echo "  $0"
    echo "  $0 --with-model"
    exit 2
    ;;
esac

[[ -d "$ROOT" ]] || {
  echo "[ERROR] benchmark root not found:"
  echo "  $ROOT"
  exit 3
}

[[ -d "$ASSETS" ]] || {
  echo "[ERROR] assets root not found:"
  echo "  $ASSETS"
  exit 3
}

RUN_DIR="$OUT_ROOT/parser-kev-${STAMP}"
PAYLOAD="$RUN_DIR/payload"
EVIDENCE="$RUN_DIR/evidence"

mkdir -p "$PAYLOAD" "$EVIDENCE"

echo "============================================================"
echo "BLINK Parser + Kev 0.8B collector"
echo "============================================================"
echo "ROOT       : $ROOT"
echo "ASSETS     : $ASSETS"
echo "OUTPUT     : $RUN_DIR"
echo "WITH_MODEL : $WITH_MODEL"
echo


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

copy_file()
{
  local src="$1"
  local dst="$2"

  [[ -f "$src" ]] || return 0

  mkdir -p "$(dirname "$dst")"
  cp -a -- "$src" "$dst"
}


copy_tree()
{
  local src="$1"
  local dst="$2"

  [[ -d "$src" ]] || return 0

  mkdir -p "$(dirname "$dst")"

  rsync -a \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.pytest_cache/' \
    --exclude='.mypy_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='.cache/' \
    --exclude='.state/' \
    --exclude='.private/' \
    --exclude='results/' \
    --exclude='*.log' \
    --exclude='*.tar' \
    --exclude='*.tar.gz' \
    --exclude='*.zip' \
    "$src/" "$dst/"
}


safe_cmd()
{
  local outfile="$1"
  shift

  {
    echo "\$ $*"
    "$@"
  } >"$outfile" 2>&1 || true
}


# ------------------------------------------------------------
# 1. Record source locations
# ------------------------------------------------------------

cat >"$EVIDENCE/source-paths.txt" <<EOF
benchmark_root=$ROOT
assets_root=$ASSETS

expected_assets:
$ASSETS/.models
$ASSETS/.runtime
$ASSETS/vendor/kev

workspace_links:
$ROOT/.models
$ROOT/.runtime
$ROOT/vendor/kev
EOF


# ------------------------------------------------------------
# 2. Capture workspace symlink layout
# ------------------------------------------------------------

{
  echo "===== ROOT ====="
  ls -la "$ROOT"
  echo

  echo "===== ASSETS ====="
  ls -la "$ASSETS"
  echo

  echo "===== WORKSPACE LINKS ====="
  for p in \
    "$ROOT/.models" \
    "$ROOT/.runtime" \
    "$ROOT/vendor/kev"
  do
    if [[ -e "$p" || -L "$p" ]]; then
      ls -ld "$p"
      readlink -f "$p" || true
    fi
  done
} >"$EVIDENCE/layout.txt" 2>&1


# ------------------------------------------------------------
# 3. Collect current Parser / policy / Kev source
# ------------------------------------------------------------
#
# We deliberately use both known paths and name-based discovery.
# This avoids losing files if the latest workspace layout changed.
#

mkdir -p "$PAYLOAD/workspace"

# Gate 1 qualification / parser material.
copy_tree \
  "$ROOT/gate1" \
  "$PAYLOAD/workspace/gate1"

# Gate 2 integration harness.
copy_tree \
  "$ROOT/gate2" \
  "$PAYLOAD/workspace/gate2"

# Current vendor Kev implementation.
copy_tree \
  "$ASSETS/vendor/kev" \
  "$PAYLOAD/assets/vendor/kev"

# Runtime helper material, but not arbitrary cache/state.
if [[ -d "$ASSETS/.runtime" ]]; then
  copy_tree \
    "$ASSETS/.runtime" \
    "$PAYLOAD/assets/runtime"
fi


# ------------------------------------------------------------
# 4. Discover relevant parser / Kev files elsewhere in workspace
# ------------------------------------------------------------

DISCOVERED="$EVIDENCE/discovered-parser-kev-files.txt"

find "$ROOT" "$ASSETS" \
  -xdev \
  \( \
       -path '*/.private/*' \
    -o -path '*/results/*' \
    -o -path '*/.cache/*' \
    -o -path '*/__pycache__/*' \
  \) -prune \
  -o \
  -type f \
  \( \
       -iname '*parser*.py' \
    -o -iname '*kev*.py' \
    -o -iname '*qualification*.py' \
    -o -iname '*mappab*.py' \
    -o -iname '*intent*.py' \
    -o -iname '*policy*.py' \
    -o -iname '*routing*.py' \
    -o -iname '*parser*.json' \
    -o -iname '*kev*.json' \
    -o -iname '*intent*.json' \
    -o -iname '*qualification*.json' \
  \) \
  -print \
  | sort \
  >"$DISCOVERED"

while IFS= read -r src
do
  [[ -n "$src" ]] || continue

  case "$src" in
    "$ROOT"/*)
      rel="${src#"$ROOT"/}"
      dst="$PAYLOAD/discovered/workspace/$rel"
      ;;

    "$ASSETS"/*)
      rel="${src#"$ASSETS"/}"
      dst="$PAYLOAD/discovered/assets/$rel"
      ;;

    *)
      continue
      ;;
  esac

  copy_file "$src" "$dst"

done <"$DISCOVERED"


# ------------------------------------------------------------
# 5. Locate Kev model directories
# ------------------------------------------------------------

MODEL_LIST="$EVIDENCE/model-directories.txt"

: >"$MODEL_LIST"

if [[ -d "$ASSETS/.models" ]]; then
  find "$ASSETS/.models" \
    -maxdepth 3 \
    -type d \
    \( \
         -iname 'kev*' \
      -o -iname '*kev-0.8b*' \
      -o -iname '*qwen*0.8b*' \
      -o -iname '*qwen3.5*' \
    \) \
    -print \
    | sort -u \
    >>"$MODEL_LIST"
fi

echo "Detected model directories:"
cat "$MODEL_LIST" || true
echo


# ------------------------------------------------------------
# 6. Model metadata
# ------------------------------------------------------------

mkdir -p "$PAYLOAD/model-metadata"

while IFS= read -r model_dir
do
  [[ -d "$model_dir" ]] || continue

  model_name="$(basename "$model_dir")"
  dst="$PAYLOAD/model-metadata/$model_name"

  mkdir -p "$dst"

  # Safe metadata/config files.
  for f in \
    config.json \
    generation_config.json \
    tokenizer_config.json \
    tokenizer.json \
    special_tokens_map.json \
    added_tokens.json \
    chat_template.json \
    adapter_config.json \
    README.md \
    LICENSE \
    LICENSE.txt
  do
    copy_file \
      "$model_dir/$f" \
      "$dst/$f"
  done

  # Inventory only. No weights copied here.
  {
    echo "model_dir=$model_dir"
    echo
    find "$model_dir" \
      -maxdepth 2 \
      -type f \
      -printf '%P\t%s bytes\n' \
      | sort
  } >"$dst/FILE_INVENTORY.txt"

done <"$MODEL_LIST"


# ------------------------------------------------------------
# 7. Optional: include full Kev model
# ------------------------------------------------------------

if [[ "$WITH_MODEL" -eq 1 ]]; then
  echo "[INFO] Full model copy enabled."

  mkdir -p "$PAYLOAD/models"

  while IFS= read -r model_dir
  do
    [[ -d "$model_dir" ]] || continue

    model_name="$(basename "$model_dir")"

    echo "[INFO] Copying model:"
    echo "  $model_dir"

    rsync -a \
      --exclude='*.lock' \
      --exclude='.cache/' \
      "$model_dir/" \
      "$PAYLOAD/models/$model_name/"

  done <"$MODEL_LIST"

else
  cat >"$PAYLOAD/MODEL_WEIGHTS_NOT_INCLUDED.txt" <<'EOF'
Kev/Qwen model weight files were intentionally not included.

Run the collector with:

  ./collect_parser_kev.sh --with-model

to include the detected Kev 0.8B model directories.

The model configuration, tokenizer metadata, adapter metadata,
file inventory and environment evidence are included in the
default package.
EOF
fi


# ------------------------------------------------------------
# 8. Python / Torch / ROCm environment
# ------------------------------------------------------------

safe_cmd "$EVIDENCE/os.txt" \
  bash -lc 'cat /etc/os-release; echo; uname -a'

safe_cmd "$EVIDENCE/rocm.txt" \
  bash -lc '
    cat /opt/rocm/.info/version 2>/dev/null || true
    echo
    /opt/rocm/bin/hipcc --version 2>/dev/null || hipcc --version 2>/dev/null || true
  '

safe_cmd "$EVIDENCE/gpu.txt" \
  bash -lc '
    /opt/rocm/bin/amd-smi static 2>/dev/null || \
    amd-smi static 2>/dev/null || true

    echo
    rocminfo 2>/dev/null | head -120 || true
  '

safe_cmd "$EVIDENCE/python-command.txt" \
  bash -lc '
    which python3 || true
    python3 --version || true
  '

python3 - <<'PY' >"$EVIDENCE/python-runtime.json" 2>&1 || true
import json
import platform
import sys

out = {
    "python": sys.executable,
    "python_version": sys.version,
    "platform": platform.platform(),
}

for module_name in (
    "torch",
    "transformers",
    "tokenizers",
    "numpy",
    "safetensors",
    "accelerate",
    "pydantic",
):
    try:
        module = __import__(module_name)
        out[module_name] = getattr(module, "__version__", "unknown")
    except Exception as exc:
        out[module_name] = {
            "status": "not_importable",
            "error_type": type(exc).__name__,
        }

try:
    import torch

    out["torch_runtime"] = {
        "version": torch.__version__,
        "hip": getattr(torch.version, "hip", None),
        "cuda": getattr(torch.version, "cuda", None),
        "cuda_available": torch.cuda.is_available(),
        "device_count": torch.cuda.device_count(),
        "devices": [
            torch.cuda.get_device_name(i)
            for i in range(torch.cuda.device_count())
        ],
    }
except Exception as exc:
    out["torch_runtime"] = {
        "status": "error",
        "error_type": type(exc).__name__,
    }

print(json.dumps(out, ensure_ascii=False, indent=2))
PY


# ------------------------------------------------------------
# 9. Package versions
# ------------------------------------------------------------

python3 -m pip freeze \
  >"$EVIDENCE/pip-freeze.txt" \
  2>&1 || true

python3 -m pip list \
  >"$EVIDENCE/pip-list.txt" \
  2>&1 || true


# ------------------------------------------------------------
# 10. Record running Kev-related processes
# ------------------------------------------------------------

{
  echo "===== Kev / intent related processes ====="

  ps -eo pid,ppid,user,lstart,args \
    | grep -Ei \
      'kev|intent|8230|qwen3\.5|qwen.*0\.8' \
    | grep -v grep \
    || true

  echo
  echo "===== Relevant listeners ====="

  ss -lntp 2>/dev/null \
    | grep -E ':(8230|8210|8203|8206|8003|8010|8200)\b' \
    || true

} >"$EVIDENCE/processes.txt"


# ------------------------------------------------------------
# 11. Search source for model references
# ------------------------------------------------------------

{
  grep -RInE \
    --exclude-dir=.private \
    --exclude-dir=results \
    --exclude-dir=.cache \
    --exclude-dir=__pycache__ \
    --exclude='*.log' \
    'kev|Qwen3\.5|0\.8B|0\.8b|prefix_cache|temperature|bfloat16|pointer|mappab|legacy.*fallback' \
    "$ROOT" \
    "$ASSETS/vendor/kev" \
    2>/dev/null \
    || true
} >"$EVIDENCE/source-reference-search.txt"


# ------------------------------------------------------------
# 12. Safety check
# ------------------------------------------------------------

# Refuse packaging if known secret locations somehow entered payload.
if find "$PAYLOAD" \
  \( \
       -path '*/.private/*' \
    -o -name 'accounts.json' \
    -o -name '*.cookie' \
    -o -name 'cookies.json' \
    -o -name '.env' \
  \) \
  -print \
  | grep -q .
then
  echo "[ERROR] Secret-like files detected inside payload."
  find "$PAYLOAD" \
    \( \
         -path '*/.private/*' \
      -o -name 'accounts.json' \
      -o -name '*.cookie' \
      -o -name 'cookies.json' \
      -o -name '.env' \
    \) \
    -print
  exit 10
fi


# ------------------------------------------------------------
# 13. Inventory
# ------------------------------------------------------------

{
  echo "BLINK Parser + Kev export"
  echo "timestamp=$(date --iso-8601=seconds)"
  echo "with_model=$WITH_MODEL"
  echo
  echo "===== payload ====="

  find "$PAYLOAD" \
    -type f \
    -printf '%P\t%s bytes\n' \
    | sort

} >"$RUN_DIR/CONTENTS.txt"


du -sh "$PAYLOAD" \
  >"$RUN_DIR/SIZE.txt"


# ------------------------------------------------------------
# 14. Manifest
# ------------------------------------------------------------

(
  cd "$RUN_DIR"

  find payload evidence \
    -type f \
    -print0 \
    | sort -z \
    | xargs -0 sha256sum

) >"$RUN_DIR/SHA256SUMS.txt"


# ------------------------------------------------------------
# 15. Archive
# ------------------------------------------------------------

ARCHIVE="$OUT_ROOT/parser-kev-${STAMP}.tar.gz"

tar \
  -C "$OUT_ROOT" \
  -czf "$ARCHIVE" \
  "$(basename "$RUN_DIR")"


# ------------------------------------------------------------
# 16. Verify archive
# ------------------------------------------------------------

gzip -t "$ARCHIVE"

tar -tzf "$ARCHIVE" \
  >"$RUN_DIR/ARCHIVE_CONTENTS.txt"


echo
echo "============================================================"
echo "DONE"
echo "============================================================"
echo
echo "Directory:"
echo "  $RUN_DIR"
echo
echo "Archive:"
echo "  $ARCHIVE"
echo
echo "Archive size:"
du -h "$ARCHIVE"
echo
echo "SHA-256:"
sha256sum "$ARCHIVE"
echo
echo "Model weights included:"
echo "  $WITH_MODEL"
echo