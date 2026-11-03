#!/usr/bin/env bash
set -Eeuo pipefail

# BLINK vLLM v0.9.2 ROCm 6.4 installer R2
#
# Correction from R1:
# - The public ROCm 6.4 PyTorch index no longer contains torch 2.7.0.
# - It currently contains torch 2.8.0+rocm6.4 and newer.
# - vLLM v0.9.2 CMake EXPECTS 2.7.0 but only WARNs on a mismatch.
# - This R2 compatibility candidate therefore builds vLLM v0.9.2 from source
#   against the available stable torch 2.8.0+rocm6.4 stack.
#
# Production Blue is not modified or rerouted.

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
CONDA="${CONDA:-/opt/conda/bin/conda}"
PROD_ENV="${PROD_ENV:-/opt/conda/envs/rocm_env}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
SRC="${VLLM_SOURCE_DIR:-$ROOT/_third_party/vllm-v0.9.2}"
REF="${VLLM_REF:-v0.9.2}"
REPO="${VLLM_REPO:-https://github.com/vllm-project/vllm.git}"
ROCM_INDEX="${ROCM_INDEX:-https://download.pytorch.org/whl/rocm6.4}"

PYTHON_VERSION="${PYTHON_VERSION:-3.10.20}"
TORCH_VERSION="${TORCH_VERSION:-2.8.0}"
TORCHVISION_VERSION="${TORCHVISION_VERSION:-0.23.0}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.8.0}"
TRANSFORMERS_VERSION="${TRANSFORMERS_VERSION:-4.53.2}"
GPU_ARCH="${GPU_ARCH:-gfx942}"
MAX_JOBS="${MAX_JOBS:-4}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT_DIR="${REPORT_DIR:-$ROOT/ops/vllm-green/install-reports-r2/$STAMP}"
LOG="$REPORT_DIR/install.log"
CONSTRAINTS="$REPORT_DIR/constraints.txt"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

run_logged() {
  echo
  printf '>>>'
  printf ' %q' "$@"
  echo
  "$@" 2>&1 | tee -a "$LOG"
}

mkdir -p "$REPORT_DIR"
touch "$LOG"

echo "===== R2 SAFETY PRECHECK =====" | tee -a "$LOG"

[[ -x "$CONDA" ]] || die "conda missing: $CONDA"
[[ -x "$PROD_ENV/bin/python" ]] || die "production Python missing"
[[ -x /opt/rocm/bin/hipcc ]] || die "ROCm hipcc missing"

listener 8000 || die "Blue :8000 must remain listening"
if listener 8200; then die "vLLM candidate :8200 already listening"; fi
for port in 8004 8005; do
  if listener "$port"; then die "retired/stale :$port is active"; fi
done

[[ "$GREEN_ENV" != "$PROD_ENV" ]] || die "Green env cannot be production env"

"$PROD_ENV/bin/python" - <<'PY' | tee -a "$LOG"
import sys, torch
print("production_python:", sys.executable)
print("production_torch:", torch.__version__)
print("production_hip:", torch.version.hip)
print("production_gpu:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] production ROCm baseline unavailable")
PY

echo
echo "===== EXACT vLLM SOURCE =====" | tee -a "$LOG"

if [[ ! -d "$SRC/.git" ]]; then
  run_logged git clone --depth 1 --branch "$REF" "$REPO" "$SRC"
fi

REMOTE_SHA="$(git ls-remote --refs --tags "$REPO" "refs/tags/$REF" | awk '{print $1}' | head -1)"
[[ -n "$REMOTE_SHA" ]] || die "upstream tag $REF missing"

HEAD="$(git -C "$SRC" rev-parse HEAD)"
DESC="$(git -C "$SRC" describe --tags --always --dirty)"
echo "remote_sha=$REMOTE_SHA" | tee -a "$LOG"
echo "head=$HEAD" | tee -a "$LOG"
echo "describe=$DESC" | tee -a "$LOG"

[[ "$HEAD" == "$REMOTE_SHA" ]] || die "source HEAD mismatch"
[[ "$DESC" == "$REF" ]] || die "source dirty or wrong tag"
grep -q 'gfx942' "$SRC/CMakeLists.txt" || die "gfx942 support absent"
grep -Rqs 'Gemma3ForConditionalGeneration' "$SRC/vllm" \
  || die "Gemma3ForConditionalGeneration support absent"

echo
echo "===== v0.9.2 TORCH VERSION CONTRACT =====" | tee -a "$LOG"
grep -n 'TORCH_SUPPORTED_VERSION_ROCM' "$SRC/CMakeLists.txt" | tee -a "$LOG"
echo "[NOTE] v0.9.2 expects ROCm Torch 2.7.0 but CMake treats a mismatch as WARNING, not fatal." \
  | tee -a "$LOG"
echo "[NOTE] R2 intentionally uses public stable torch 2.8.0+rocm6.4." \
  | tee -a "$LOG"

echo
echo "===== GREEN ENV =====" | tee -a "$LOG"

if [[ ! -e "$GREEN_ENV" ]]; then
  run_logged "$CONDA" create -y --prefix "$GREEN_ENV" \
    "python=$PYTHON_VERSION" pip "setuptools<80" wheel packaging
else
  [[ -x "$GREEN_ENV/bin/python" ]] || die "existing Green env is invalid"
  echo "[REUSE] $GREEN_ENV from the failed R1 attempt" | tee -a "$LOG"
fi

PY="$GREEN_ENV/bin/python"
export PATH="$GREEN_ENV/bin:$PATH"

echo
echo "===== VERIFY GREEN IS SAFE TO RESUME =====" | tee -a "$LOG"
"$PY" - <<'PY' | tee -a "$LOG"
import importlib.util, sys
print("python:", sys.executable)
for name in ("vllm",):
    present = importlib.util.find_spec(name) is not None
    print(f"{name}_present:", present)
    if present:
        raise SystemExit("[ERROR] vLLM already installed; refusing ambiguous resume")
try:
    import torch
except ImportError:
    print("torch: not installed (expected after R1 failure)")
else:
    print("torch:", torch.__version__)
    if not torch.__version__.startswith("2.8.0"):
        raise SystemExit("[ERROR] existing Green torch is not the R2 candidate")
PY

echo
echo "===== INSTALL PUBLIC ROCm 6.4 PYTORCH 2.8 STACK =====" | tee -a "$LOG"

run_logged "$PY" -m pip install \
  "torch==$TORCH_VERSION" \
  "torchvision==$TORCHVISION_VERSION" \
  "torchaudio==$TORCHAUDIO_VERSION" \
  --index-url "$ROCM_INDEX"

"$PY" - <<'PY' | tee -a "$LOG"
import sys, torch
print("python:", sys.executable)
print("torch:", torch.__version__)
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("available:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] installed PyTorch is not ROCm/HIP")
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit(f"[ERROR] unexpected Torch {torch.__version__}")
if "MI300" not in torch.cuda.get_device_name(0):
    raise SystemExit("[ERROR] expected MI300X")
PY

cat > "$CONSTRAINTS" <<EOF
torch==2.8.0+rocm6.4
torchvision==0.23.0+rocm6.4
torchaudio==2.8.0+rocm6.4
transformers==$TRANSFORMERS_VERSION
EOF

echo
echo "===== BUILD / RUNTIME PREREQUISITES =====" | tee -a "$LOG"

run_logged "$PY" -m pip install \
  "transformers==$TRANSFORMERS_VERSION" \
  "cmake>=3.26.1" \
  ninja \
  wheel \
  pybind11 \
  "packaging>=24.2" \
  "setuptools>=77.0.3,<80.0.0" \
  "setuptools-scm>=8" \
  jinja2

echo
echo "===== ROCm REQUIREMENTS =====" | tee -a "$LOG"

# rocm.txt does not pin torch. Install inside the isolated candidate env.
run_logged "$PY" -m pip install \
  -c "$CONSTRAINTS" \
  -r "$SRC/requirements/rocm.txt"

echo
echo "===== REASSERT CORE STACK =====" | tee -a "$LOG"

run_logged "$PY" -m pip install \
  -c "$CONSTRAINTS" \
  "transformers==$TRANSFORMERS_VERSION"

"$PY" - <<'PY' | tee -a "$LOG"
import torch, transformers
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("hip:", torch.version.hip)
print("gpu:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit("[ERROR] requirements changed Torch")
if transformers.__version__ != "4.53.2":
    raise SystemExit("[ERROR] Transformers drift")
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] ROCm runtime lost")
PY

echo
echo "===== TRITON INVENTORY =====" | tee -a "$LOG"

"$PY" - <<'PY' | tee -a "$LOG"
import importlib.metadata as md
import torch
try:
    import triton
    print("triton:", triton.__version__)
except Exception as exc:
    raise SystemExit(f"[ERROR] Triton import failed: {type(exc).__name__}: {exc}")
for pkg in ("pytorch-triton-rocm", "triton"):
    try:
        print(pkg + ":", md.version(pkg))
    except md.PackageNotFoundError:
        pass
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
PY

echo
echo "===== PRE-BUILD LINKER / ARCH TARGETS =====" | tee -a "$LOG"

TORCH_LIB="$("$PY" - <<'PY'
from pathlib import Path
import torch
print(Path(torch.__file__).resolve().parent / "lib")
PY
)"
[[ -f "$TORCH_LIB/libc10.so" ]] || die "libc10.so missing from $TORCH_LIB"

export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export VLLM_TARGET_DEVICE=rocm
export PYTORCH_ROCM_ARCH="$GPU_ARCH"
export GPU_ARCHS="$GPU_ARCH"
export AMDGPU_TARGETS="$GPU_ARCH"
export ROCM_PATH=/opt/rocm
export MAX_JOBS
export CMAKE_BUILD_PARALLEL_LEVEL="$MAX_JOBS"

# ROCm 6.4 can fail automatic architecture detection. setup.py forwards
# CMAKE_ARGS, so explicitly seed HIP's GPU_TARGETS CMake variable as well.
export CMAKE_ARGS="${CMAKE_ARGS:-} -DGPU_TARGETS=$GPU_ARCH"

echo "torch_lib=$TORCH_LIB" | tee -a "$LOG"
echo "PYTORCH_ROCM_ARCH=$PYTORCH_ROCM_ARCH" | tee -a "$LOG"
echo "GPU_ARCHS=$GPU_ARCHS" | tee -a "$LOG"
echo "AMDGPU_TARGETS=$AMDGPU_TARGETS" | tee -a "$LOG"
echo "CMAKE_ARGS=$CMAKE_ARGS" | tee -a "$LOG"

echo
echo "===== SOURCE BUILD vLLM v0.9.2 =====" | tee -a "$LOG"

pushd "$SRC" >/dev/null
run_logged "$PY" setup.py develop
popd >/dev/null

echo
echo "===== VERIFY vLLM / ROCm EXTENSION =====" | tee -a "$LOG"

LD_LIBRARY_PATH="$LD_LIBRARY_PATH" "$PY" - <<'PY' | tee -a "$LOG"
import sys, torch, transformers
print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("hip:", torch.version.hip)
print("gpu:", torch.cuda.get_device_name(0))

import vllm
print("vllm:", vllm.__version__)

import vllm._C
print("vllm._C:", vllm._C.__file__)

if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] ROCm unavailable after build")
print("[PASS] vLLM extension import")
PY

echo
echo "===== PIP CHECK =====" | tee -a "$LOG"
"$PY" -m pip check 2>&1 | tee "$REPORT_DIR/pip-check.txt"

"$PY" -m pip freeze > "$REPORT_DIR/pip-freeze.txt"
"$CONDA" list --prefix "$GREEN_ENV" > "$REPORT_DIR/conda-list.txt"
git -C "$SRC" status --short > "$REPORT_DIR/source-status.txt"

echo
echo "[OK] R2 vLLM v0.9.2 / Torch 2.8 ROCm 6.4 candidate prepared." | tee -a "$LOG"
echo "Green env : $GREEN_ENV" | tee -a "$LOG"
echo "Source    : $SRC" | tee -a "$LOG"
echo "Torch     : 2.8.0+rocm6.4" | tee -a "$LOG"
echo "GPU arch  : $GPU_ARCH" | tee -a "$LOG"
echo "Report    : $REPORT_DIR" | tee -a "$LOG"
echo
echo "Production Blue was not restarted or rerouted."
