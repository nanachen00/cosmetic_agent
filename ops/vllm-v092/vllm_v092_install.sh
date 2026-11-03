#!/usr/bin/env bash
set -Eeuo pipefail

# vLLM v0.9.2 isolated ROCm 6.4 source-build installer.
# Creates a brand-new Green candidate environment and never modifies Blue.

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
CONDA="${CONDA:-/opt/conda/bin/conda}"
PROD_ENV="${PROD_ENV:-/opt/conda/envs/rocm_env}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
SRC="${VLLM_SOURCE_DIR:-$ROOT/_third_party/vllm-v0.9.2}"
REF="${VLLM_REF:-v0.9.2}"
REPO="${VLLM_REPO:-https://github.com/vllm-project/vllm.git}"
ROCM_INDEX="${ROCM_INDEX:-https://download.pytorch.org/whl/rocm6.4}"

PYTHON_VERSION="${PYTHON_VERSION:-3.10.20}"
TORCH_VERSION="${TORCH_VERSION:-2.7.0}"
TORCHVISION_VERSION="${TORCHVISION_VERSION:-0.22.0}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.7.0}"
TRANSFORMERS_VERSION="${TRANSFORMERS_VERSION:-4.53.2}"
GPU_ARCH="${GPU_ARCH:-gfx942}"
MAX_JOBS="${MAX_JOBS:-4}"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT_DIR="${REPORT_DIR:-$ROOT/ops/vllm-green/install-reports/$STAMP}"
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

echo "===== SAFETY PRECHECK =====" | tee -a "$LOG"
[[ -x "$CONDA" ]] || die "conda missing: $CONDA"
[[ -x "$PROD_ENV/bin/python" ]] || die "production Python missing"
[[ -x /opt/rocm/bin/hipcc ]] || die "ROCm hipcc missing"
listener 8000 || die "Blue :8000 must remain listening"
if listener 8200; then die "candidate :8200 is already listening"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p is active"; fi
done
[[ "$GREEN_ENV" != "$PROD_ENV" ]] || die "Green env must differ from production env"
[[ ! -e "$GREEN_ENV" ]] || die "Green env already exists: $GREEN_ENV"

echo
echo "===== VERIFY EXACT vLLM SOURCE =====" | tee -a "$LOG"
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
[[ "$DESC" == "$REF" ]] || die "source dirty/wrong tag"
grep -q 'gfx942' "$SRC/CMakeLists.txt" || die "gfx942 support absent"
grep -q 'TORCH_SUPPORTED_VERSION_ROCM "2.7.0"' "$SRC/CMakeLists.txt" \
  || die "v0.9.2 ROCm Torch 2.7.0 contract absent"
grep -Rqs 'Gemma3ForConditionalGeneration' "$SRC/vllm" \
  || die "Gemma3ForConditionalGeneration support absent"

echo
echo "===== CREATE FRESH GREEN ENV =====" | tee -a "$LOG"
run_logged "$CONDA" create -y --prefix "$GREEN_ENV" \
  "python=$PYTHON_VERSION" pip "setuptools<80" wheel packaging

PY="$GREEN_ENV/bin/python"
export PATH="$GREEN_ENV/bin:$PATH"

echo
echo "===== INSTALL PINNED PYTORCH ROCm 6.4 =====" | tee -a "$LOG"
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
    raise SystemExit("[ERROR] ROCm torch unavailable")
if not torch.__version__.startswith("2.7.0"):
    raise SystemExit(f"[ERROR] unexpected torch {torch.__version__}")
if "MI300" not in torch.cuda.get_device_name(0):
    raise SystemExit("[ERROR] expected MI300X")
PY

cat > "$CONSTRAINTS" <<EOF
transformers==$TRANSFORMERS_VERSION
EOF

echo
echo "===== PIN TRANSFORMERS / BUILD TOOLCHAIN =====" | tee -a "$LOG"
run_logged "$PY" -m pip install \
  -c "$CONSTRAINTS" \
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
echo "===== VERIFY PYTORCH-BUNDLED TRITON =====" | tee -a "$LOG"
"$PY" - <<'PY' | tee -a "$LOG"
import importlib.metadata as md
import torch
try:
    import triton
    print("triton_module:", triton.__version__)
except Exception as exc:
    raise SystemExit(f"[ERROR] Triton import failed: {type(exc).__name__}: {exc}")

for name in ("pytorch-triton-rocm", "triton"):
    try:
        print(name + ":", md.version(name))
    except md.PackageNotFoundError:
        pass
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
PY

echo
echo "===== OPTIONAL AMD-SMI PYTHON PACKAGE =====" | tee -a "$LOG"
if [[ -d /opt/rocm/share/amd_smi ]]; then
  run_logged "$PY" -m pip install --no-deps /opt/rocm/share/amd_smi
else
  echo "[NOTE] /opt/rocm/share/amd_smi not present; skipping" | tee -a "$LOG"
fi

echo
echo "===== INSTALL vLLM ROCm REQUIREMENTS =====" | tee -a "$LOG"
# Requirements are installed inside the isolated candidate env only.
# The Transformers constraint prevents today's newest release from drifting
# away from the v0.9.2-era compatibility baseline.
run_logged "$PY" -m pip install \
  -c "$CONSTRAINTS" \
  -r "$SRC/requirements/rocm.txt"

echo
echo "===== REASSERT CORE VERSION CONTRACT =====" | tee -a "$LOG"
run_logged "$PY" -m pip install \
  -c "$CONSTRAINTS" \
  "transformers==$TRANSFORMERS_VERSION"

"$PY" - <<'PY' | tee -a "$LOG"
import torch, transformers
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("hip:", torch.version.hip)
if not torch.__version__.startswith("2.7.0"):
    raise SystemExit("[ERROR] requirements changed Torch")
if transformers.__version__ != "4.53.2":
    raise SystemExit("[ERROR] unexpected Transformers version")
if torch.version.hip is None or not torch.cuda.is_available():
    raise SystemExit("[ERROR] ROCm runtime lost")
PY

echo
echo "===== PRE-BUILD DYNAMIC LINKER =====" | tee -a "$LOG"
TORCH_LIB="$("$PY" - <<'PY'
from pathlib import Path
import torch
print(Path(torch.__file__).resolve().parent / "lib")
PY
)"
[[ -f "$TORCH_LIB/libc10.so" ]] || die "libc10.so missing: $TORCH_LIB"
export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
echo "torch_lib=$TORCH_LIB" | tee -a "$LOG"

echo
echo "===== BUILD vLLM v0.9.2 FOR ROCm gfx942 =====" | tee -a "$LOG"
export VLLM_TARGET_DEVICE=rocm
export PYTORCH_ROCM_ARCH="$GPU_ARCH"
export ROCM_PATH=/opt/rocm
export MAX_JOBS
export CMAKE_BUILD_PARALLEL_LEVEL="$MAX_JOBS"

pushd "$SRC" >/dev/null
run_logged "$PY" setup.py develop
popd >/dev/null

echo
echo "===== FINAL VERIFY =====" | tee -a "$LOG"
LD_LIBRARY_PATH="$LD_LIBRARY_PATH" "$PY" "$(dirname "$0")/vllm_v092_verify.py" \
  2>&1 | tee -a "$LOG"

echo
echo "===== PIP CHECK =====" | tee -a "$LOG"
"$PY" -m pip check 2>&1 | tee "$REPORT_DIR/pip-check.txt"

"$PY" -m pip freeze > "$REPORT_DIR/pip-freeze.txt"
"$CONDA" list --prefix "$GREEN_ENV" > "$REPORT_DIR/conda-list.txt"
git -C "$SRC" status --short > "$REPORT_DIR/source-status.txt"

echo
echo "[OK] vLLM v0.9.2 isolated ROCm runtime prepared." | tee -a "$LOG"
echo "Green env : $GREEN_ENV" | tee -a "$LOG"
echo "Source    : $SRC" | tee -a "$LOG"
echo "Torch     : $TORCH_VERSION / ROCm 6.4" | tee -a "$LOG"
echo "Transformers: $TRANSFORMERS_VERSION" | tee -a "$LOG"
echo "GPU arch  : $GPU_ARCH" | tee -a "$LOG"
echo "Report    : $REPORT_DIR" | tee -a "$LOG"
echo
echo "No production service was restarted or rerouted."
echo "Next: ./vllm_v092_start_8200.sh"
