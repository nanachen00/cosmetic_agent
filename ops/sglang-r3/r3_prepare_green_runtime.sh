#!/usr/bin/env bash
set -Eeuo pipefail

# BLINK SGLang Green R3 installer
# Fresh isolated stack aligned to SGLang v0.5.1.post2 ROCm/AITER path.
#
# Production Blue is never modified:
#   /opt/conda/envs/rocm_env
#   :8000
#
# Green:
#   /opt/conda/envs/sglang-rocm-v051p2
#   :8100

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
CONDA="${CONDA:-/opt/conda/bin/conda}"
PROD_ENV="${PROD_ENV:-/opt/conda/envs/rocm_env}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm-v051p2}"
PYTHON_VERSION="${PYTHON_VERSION:-3.10.20}"

SGLANG_REPO="${SGLANG_REPO:-https://github.com/sgl-project/sglang.git}"
SGLANG_REF="${SGLANG_REF:-v0.5.1.post2}"
SGLANG_HEAD="${SGLANG_HEAD:-e3e97a120bf7caebc1fad10880782900209d9cb4}"
SGLANG_SRC="${SGLANG_SRC:-$ROOT/_third_party/sglang-v0.5.1.post2-probe-20260921-151422}"

AITER_REPO="${AITER_REPO:-https://github.com/ROCm/aiter.git}"
AITER_REF="${AITER_REF:-v0.1.4}"
AITER_SRC="${AITER_SRC:-$ROOT/_third_party/aiter-v0.1.4}"

ROCM_INDEX="${ROCM_INDEX:-https://download.pytorch.org/whl/rocm6.4}"
TORCH_VERSION="${TORCH_VERSION:-2.8.0}"
TORCHVISION_VERSION="${TORCHVISION_VERSION:-0.23.0}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.8.0}"
TRANSFORMERS_VERSION="${TRANSFORMERS_VERSION:-4.55.2}"

ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
GPU_ARCH="${GPU_ARCH:-gfx942}"
MAX_JOBS="${MAX_JOBS:-4}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT_DIR="${REPORT_DIR:-$ROOT/ops/sglang-green/r3-install-reports/$STAMP}"
LOG="$REPORT_DIR/install.log"
CONSTRAINTS="$REPORT_DIR/constraints.txt"

die() {
  echo "[ERROR] $*" >&2
  exit 2
}

listener() {
  ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .
}

run_logged() {
  echo
  printf '>>>'
  printf ' %q' "$@"
  echo
  "$@" 2>&1 | tee -a "$LOG"
}

mkdir -p "$REPORT_DIR"
touch "$LOG"

echo "===== SAFETY PRECHECK =====" | tee -a "$LOG"

[[ -x "$CONDA" ]] || die "conda missing: $CONDA"
[[ -x "$PROD_ENV/bin/python" ]] || die "production Python missing: $PROD_ENV/bin/python"
[[ -x "$ROCM_PATH/bin/hipcc" ]] || die "hipcc missing: $ROCM_PATH/bin/hipcc"

listener 8000 || die "Blue :8000 must remain listening"
if listener 8100; then
  die "Green :8100 is already listening"
fi
for p in 8004 8005; do
  if listener "$p"; then
    die "retired/stale :$p is unexpectedly active"
  fi
done

if [[ "$GREEN_ENV" == "$PROD_ENV" ]]; then
  die "Green environment must not be production environment"
fi

for cmd in git cmake make gcc g++ sha256sum; do
  command -v "$cmd" >/dev/null 2>&1 || die "required build command missing: $cmd"
done

"$PROD_ENV/bin/python" - <<'PY' | tee -a "$LOG"
import sys, torch, transformers
print("production_python:", sys.executable)
print("production_torch:", torch.__version__)
print("production_transformers:", transformers.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] production runtime is not ROCm-capable")
PY

"$ROCM_PATH/bin/hipcc" --version | tee -a "$LOG"

if [[ -e "$GREEN_ENV" ]]; then
  die "R3 Green env already exists: $GREEN_ENV. Use r3_remove_green_env.sh only if you intentionally want a clean retry."
fi

echo
echo "===== VERIFY SGLANG SOURCE =====" | tee -a "$LOG"
if [[ ! -d "$SGLANG_SRC/.git" ]]; then
  run_logged git clone --depth 1 --branch "$SGLANG_REF" "$SGLANG_REPO" "$SGLANG_SRC"
fi

SGLANG_ACTUAL_HEAD="$(git -C "$SGLANG_SRC" rev-parse HEAD)"
SGLANG_DESC="$(git -C "$SGLANG_SRC" describe --tags --always --dirty)"
echo "sglang_source=$SGLANG_SRC" | tee -a "$LOG"
echo "sglang_head=$SGLANG_ACTUAL_HEAD" | tee -a "$LOG"
echo "sglang_describe=$SGLANG_DESC" | tee -a "$LOG"

[[ "$SGLANG_ACTUAL_HEAD" == "$SGLANG_HEAD" ]] || die "unexpected SGLang HEAD"
[[ "$SGLANG_DESC" == "$SGLANG_REF" ]] || die "SGLang checkout dirty or wrong tag"

grep -Rqs 'Gemma3ForConditionalGeneration' "$SGLANG_SRC/python/sglang" \
  || die "Gemma3ForConditionalGeneration support missing"

grep -q 'default_target = "gfx942"' "$SGLANG_SRC/sgl-kernel/setup_rocm.py" \
  || die "expected gfx942 ROCm target not found"

echo
echo "===== CREATE FRESH GREEN ENV =====" | tee -a "$LOG"
run_logged "$CONDA" create -y --prefix "$GREEN_ENV" \
  "python=$PYTHON_VERSION" pip setuptools wheel packaging

PY="$GREEN_ENV/bin/python"
PIP=("$PY" -m pip)

echo
echo "===== INSTALL PINNED PYTORCH ROCm 6.4 =====" | tee -a "$LOG"
run_logged "${PIP[@]}" install \
  "torch==$TORCH_VERSION" \
  "torchvision==$TORCHVISION_VERSION" \
  "torchaudio==$TORCHAUDIO_VERSION" \
  --index-url "$ROCM_INDEX"

"$PY" - <<'PY' | tee -a "$LOG"
import torch, sys
print("python:", sys.executable)
print("torch:", torch.__version__)
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("available:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available():
    raise SystemExit("[ERROR] ROCm torch has no accelerator")
if torch.version.hip is None:
    raise SystemExit("[ERROR] installed torch is not ROCm/HIP")
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit(f"[ERROR] unexpected torch version {torch.__version__}")
if "MI300" not in torch.cuda.get_device_name(0):
    raise SystemExit("[ERROR] unexpected accelerator")
PY

cat > "$CONSTRAINTS" <<EOF
torch==2.8.0+rocm6.4
torchvision==0.23.0+rocm6.4
torchaudio==2.8.0+rocm6.4
transformers==$TRANSFORMERS_VERSION
EOF

echo
echo "===== INSTALL COMMON BUILD/RUNTIME PREREQUISITES =====" | tee -a "$LOG"
run_logged "${PIP[@]}" install \
  -c "$CONSTRAINTS" \
  "transformers==$TRANSFORMERS_VERSION" \
  tomli \
  ninja \
  psutil \
  pandas \
  pybind11 \
  setuptools_scm \
  cmake \
  einops

echo
echo "===== BUILD / INSTALL SGL-KERNEL 0.3.6.post2 FOR gfx942 =====" | tee -a "$LOG"
pushd "$SGLANG_SRC/sgl-kernel" >/dev/null
export ROCM_PATH
export PYTORCH_ROCM_ARCH="$GPU_ARCH"
export AMDGPU_TARGET="$GPU_ARCH"
export MAX_JOBS

run_logged "$PY" setup_rocm.py install
popd >/dev/null

"$PY" - <<'PY' | tee -a "$LOG"
import importlib.metadata as md
print("sgl-kernel:", md.version("sgl-kernel"))
if md.version("sgl-kernel") != "0.3.6.post2":
    raise SystemExit("[ERROR] unexpected sgl-kernel version")
PY

echo
echo "===== VERIFY / CLONE AITER v0.1.4 =====" | tee -a "$LOG"
if [[ ! -d "$AITER_SRC/.git" ]]; then
  run_logged git clone \
    --depth 1 \
    --branch "$AITER_REF" \
    --recurse-submodules \
    --shallow-submodules \
    "$AITER_REPO" "$AITER_SRC"
fi

AITER_DESC="$(git -C "$AITER_SRC" describe --tags --always --dirty)"
echo "aiter_source=$AITER_SRC" | tee -a "$LOG"
echo "aiter_describe=$AITER_DESC" | tee -a "$LOG"
[[ "$AITER_DESC" == "$AITER_REF" ]] || die "AITER checkout dirty or wrong tag"

# Ensure all submodules exist even if a previous interrupted clone is reused.
run_logged git -C "$AITER_SRC" submodule sync --recursive
run_logged git -C "$AITER_SRC" submodule update --init --recursive

echo
echo "===== INSTALL AITER REQUIREMENTS =====" | tee -a "$LOG"
if [[ -f "$AITER_SRC/requirements.txt" ]]; then
  run_logged "${PIP[@]}" install \
    -c "$CONSTRAINTS" \
    -r "$AITER_SRC/requirements.txt"
fi

# Reassert the desired ROCm stack after AITER requirements.
run_logged "${PIP[@]}" install \
  -c "$CONSTRAINTS" \
  "transformers==$TRANSFORMERS_VERSION"

echo
echo "===== BUILD AITER v0.1.4 FOR gfx942 =====" | tee -a "$LOG"
pushd "$AITER_SRC" >/dev/null

export AITER_USE_SYSTEM_TRITON=1
export BUILD_TARGET=rocm
export PREBUILD_KERNELS=1
export GPU_ARCHS="$GPU_ARCH"
export MAX_JOBS

run_logged "$PY" setup.py build_ext --inplace

run_logged "${PIP[@]}" install \
  --config-settings editable_mode=compat \
  -e .

popd >/dev/null

echo
echo "===== INSTALL SGLANG v0.5.1.post2 ALL_HIP =====" | tee -a "$LOG"

# A dry-run report is retained for audit. This isolated env is pinned by constraints.
"${PIP[@]}" install \
  --dry-run \
  --report "$REPORT_DIR/sglang-pip-plan.json" \
  -c "$CONSTRAINTS" \
  -e "$SGLANG_SRC/python[all_hip]" \
  2>&1 | tee "$REPORT_DIR/sglang-pip-dry-run.log"

run_logged "${PIP[@]}" install \
  -c "$CONSTRAINTS" \
  -e "$SGLANG_SRC/python[all_hip]"

echo
echo "===== FINAL VERIFY =====" | tee -a "$LOG"

SGLANG_USE_AITER=1 "$PY" "$(dirname "$0")/r3_verify_runtime.py" \
  2>&1 | tee -a "$LOG"

"${PIP[@]}" check 2>&1 | tee "$REPORT_DIR/pip-check.txt"

"$PY" -m pip freeze > "$REPORT_DIR/pip-freeze.txt"
"$CONDA" list --prefix "$GREEN_ENV" > "$REPORT_DIR/conda-list.txt"

echo
echo "[OK] R3 SGLang Green runtime prepared." | tee -a "$LOG"
echo "Green env : $GREEN_ENV" | tee -a "$LOG"
echo "SGLang   : $SGLANG_REF @ $SGLANG_HEAD" | tee -a "$LOG"
echo "AITER    : $AITER_REF" | tee -a "$LOG"
echo "Torch    : $TORCH_VERSION ROCm 6.4" | tee -a "$LOG"
echo "Report   : $REPORT_DIR" | tee -a "$LOG"
echo
echo "No service was rerouted or restarted."
echo "Next: ./r3_start_sglang_green.sh"
