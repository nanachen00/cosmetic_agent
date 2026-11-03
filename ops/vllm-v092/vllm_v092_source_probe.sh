#!/usr/bin/env bash
set -Eeuo pipefail

# vLLM v0.9.2 source-only compatibility probe for BLINK / MI300X / ROCm 6.4.
# This script does NOT install/uninstall packages or start/stop/re-route services.
# It may create:
#   - a clean source checkout under _third_party
#   - a text report under ops/vllm-green/probe-reports

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
REPO="${VLLM_REPO:-https://github.com/vllm-project/vllm.git}"
REF="${VLLM_REF:-v0.9.2}"
SRC="${VLLM_SOURCE_DIR:-$ROOT/_third_party/vllm-v0.9.2}"
PROD_PY="${PROD_PY:-/opt/conda/envs/rocm_env/bin/python}"
MODEL="${TAIDE_MODEL_PATH:-$ROOT/taide-12b}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT_DIR="${REPORT_DIR:-$ROOT/ops/vllm-green/probe-reports/$STAMP}"
REPORT="$REPORT_DIR/probe.txt"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

mkdir -p "$REPORT_DIR"

exec > >(tee "$REPORT") 2>&1

echo "===== SAFETY / PORTS ====="
listener 8000 || die "Blue :8000 must be listening"
if listener 8200; then die "vLLM candidate :8200 must be free"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p is unexpectedly active"; fi
done

for p in 8000 8001 8002 8003 8004 8005 8010 8011 8100 8200; do
  if listener "$p"; then
    echo ":$p LISTENING"
  else
    echo ":$p DOWN/FREE"
  fi
done

echo
echo "===== HOST ROCm / GPU ====="
[[ -x /opt/rocm/bin/hipcc ]] || die "/opt/rocm/bin/hipcc missing"
/opt/rocm/bin/hipcc --version | sed -n '1,8p'

if command -v amd-smi >/dev/null 2>&1; then
  amd-smi metric -g 0 -u -m || true
fi

echo
echo "===== PRODUCTION PYTORCH BASELINE ====="
[[ -x "$PROD_PY" ]] || die "production Python missing: $PROD_PY"
"$PROD_PY" - <<'PY'
import sys, torch, transformers
print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0))
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] production runtime is not ROCm/HIP-capable")
PY

echo
echo "===== TAIDE MODEL CONFIG ====="
python3 - "$MODEL" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]).resolve()
cfgp = p / "config.json"
if not cfgp.is_file():
    raise SystemExit(f"[ERROR] missing {cfgp}")
cfg = json.loads(cfgp.read_text())
text = cfg.get("text_config") or {}
print("path:", p)
print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("text_config.model_type:", text.get("model_type"))
print("text_config.max_position_embeddings:", text.get("max_position_embeddings"))
if cfg.get("architectures") != ["Gemma3ForConditionalGeneration"]:
    raise SystemExit("[ERROR] unexpected TAIDE architecture")
PY

echo
echo "===== VERIFY vLLM UPSTREAM TAG ====="
REMOTE_SHA="$(git ls-remote --refs --tags "$REPO" "refs/tags/$REF" | awk '{print $1}' | head -1)"
[[ -n "$REMOTE_SHA" ]] || die "upstream tag $REF not found"
echo "ref=$REF"
echo "remote_sha=$REMOTE_SHA"

echo
echo "===== SOURCE CHECKOUT ====="
if [[ ! -e "$SRC" ]]; then
  git clone --depth 1 --branch "$REF" "$REPO" "$SRC"
fi
[[ -d "$SRC/.git" ]] || die "source path is not a Git checkout: $SRC"

HEAD="$(git -C "$SRC" rev-parse HEAD)"
DESC="$(git -C "$SRC" describe --tags --always --dirty)"
echo "source_dir=$SRC"
echo "head=$HEAD"
echo "describe=$DESC"

[[ "$HEAD" == "$REMOTE_SHA" ]] || die "local HEAD differs from upstream tag"
[[ "$DESC" == "$REF" ]] || die "source checkout is dirty or wrong tag"

echo
echo "===== v0.9.2 PYPROJECT CONTRACT ====="
python3 - "$SRC/pyproject.toml" <<'PY'
import sys
from pathlib import Path
try:
    import tomllib as toml
except ModuleNotFoundError:
    try:
        import tomli as toml
    except ModuleNotFoundError:
        from pip._vendor import tomli as toml

p = Path(sys.argv[1])
with p.open("rb") as f:
    d = toml.load(f)
build = d.get("build-system", {})
proj = d.get("project", {})
print("requires-python:", proj.get("requires-python"))
print("build-system.requires:")
for x in build.get("requires", []):
    print(" ", x)
PY

echo
echo "===== ROCm REQUIREMENTS ====="
sed -n '1,220p' "$SRC/requirements/rocm.txt"
echo
echo "===== COMMON REQUIREMENTS (MODEL-SENSITIVE ENTRIES) ====="
grep -nE 'transformers|tokenizers|openai|xgrammar|outlines|compressed-tensors' \
  "$SRC/requirements/common.txt" || true

echo
echo "===== CMAKE ROCm CONTRACT ====="
grep -nE \
'HIP_SUPPORTED_ARCHS|TORCH_SUPPORTED_VERSION_ROCM|PYTHON_SUPPORTED_VERSIONS|VLLM_TARGET_DEVICE' \
  "$SRC/CMakeLists.txt" | head -80

grep -q 'gfx942' "$SRC/CMakeLists.txt" || die "gfx942 not listed in vLLM CMake"
grep -q 'TORCH_SUPPORTED_VERSION_ROCM "2.7.0"' "$SRC/CMakeLists.txt" \
  || die "expected ROCm Torch 2.7.0 contract not found"

echo
echo "===== GEMMA 3 SUPPORT IN EXACT SOURCE ====="
GEMMA_HITS="$(
  grep -RIn --exclude-dir=.git \
    -E 'Gemma3ForConditionalGeneration|Gemma3ForCausalLM' \
    "$SRC/vllm" 2>/dev/null | head -100 || true
)"
[[ -n "$GEMMA_HITS" ]] || die "Gemma3 architecture references not found in v0.9.2 source"
printf '%s\n' "$GEMMA_HITS"

echo
echo "===== ROCm DOCKER/SOURCE BUILD PINS ====="
for f in "$SRC/docker/Dockerfile.rocm_base" "$SRC/docker/Dockerfile.rocm"; do
  if [[ -f "$f" ]]; then
    echo "--- ${f#$SRC/} ---"
    grep -nE \
      '^ARG (BASE_IMAGE|TRITON_BRANCH|TRITON_REPO|PYTORCH_BRANCH|AITER_BRANCH|AITER_REPO|FA_BRANCH|FA_REPO)|PYTORCH_ROCM_ARCH|VLLM_TARGET_DEVICE' \
      "$f" | head -120 || true
  fi
done

echo
echo "===== BUILD ENTRYPOINT / ROCm DETECTION ====="
grep -nE \
  'VLLM_TARGET_DEVICE|torch.version.hip|ROCM_HOME|setup.py develop|_is_hip' \
  "$SRC/setup.py" | head -120 || true

echo
echo "===== BUILD TOOLS ====="
for cmd in git cmake ninja gcc g++ make; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf '%-8s %s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf '%-8s %s\n' "$cmd" "MISSING"
  fi
done

echo
echo "===== STORAGE ====="
df -h /opt/conda "$ROOT" | awk 'NR==1 || !seen[$1]++'

echo
echo "===== RESULT ====="
echo "[PASS] vLLM v0.9.2 source compatibility probe completed."
echo "No package was installed/removed."
echo "No service was started/stopped/rerouted."
echo "Source checkout: $SRC"
echo "Report: $REPORT"
