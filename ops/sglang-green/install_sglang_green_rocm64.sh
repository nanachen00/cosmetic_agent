#!/usr/bin/env bash
set -Eeuo pipefail

PROD_ENV="${PROD_ENV:-/opt/conda/envs/rocm_env}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm}"
CONDA="${CONDA:-/opt/conda/bin/conda}"
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
REPORT_DIR="${REPORT_DIR:-$ROOT/ops/sglang-green/install-reports}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT="$REPORT_DIR/$STAMP"
WHEELHOUSE="$REPORT/wheelhouse"

SGLANG_VERSION="${SGLANG_VERSION:-0.5.0}"
SGL_KERNEL_VERSION="${SGL_KERNEL_VERSION:-0.3.4}"

# Current official PEP-503 indexes for the legacy ROCm 6.4 wheel lane.
SGLANG_INDEX="${SGLANG_INDEX:-https://docs.sglang.ai/sgl-whl/rocm6.4}"
SGL_KERNEL_INDEX="${SGL_KERNEL_INDEX:-https://docs.sglang.ai/sgl-whl/kernel/rocm6.4}"
PYPI_INDEX="${PYPI_INDEX:-https://pypi.org/simple}"

die() { echo "[ERROR] $*" >&2; exit 2; }

listener() {
  ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .
}

[[ -x "$CONDA" ]] || die "conda not found: $CONDA"
[[ -x "$PROD_ENV/bin/python" ]] || die "production env missing: $PROD_ENV"

if listener 8100; then
  die ":8100 is already listening"
fi
if ! listener 8000; then
  die "Blue :8000 must remain up"
fi
for p in 8004 8005; do
  if listener "$p"; then
    die "retired/stale :$p is unexpectedly active"
  fi
done

mkdir -p "$REPORT_DIR"
mkdir "$REPORT"
mkdir "$WHEELHOUSE"

echo "===== SOURCE RUNTIME =====" | tee "$REPORT/install.log"
"$PROD_ENV/bin/python" - <<'PY' | tee -a "$REPORT/install.log"
import sys, torch, transformers
print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] source environment is not ROCm-capable")
PY

"$CONDA" list --prefix "$PROD_ENV" > "$REPORT/prod-conda-list.txt"
"$PROD_ENV/bin/python" -m pip freeze > "$REPORT/prod-pip-freeze.txt"

# The previous failed installer may already have created a clean clone.
if [[ ! -e "$GREEN_ENV" ]]; then
  echo "===== CLONE PRODUCTION ENV -> GREEN =====" | tee -a "$REPORT/install.log"
  "$CONDA" create -y --prefix "$GREEN_ENV" --clone "$PROD_ENV" \
    2>&1 | tee -a "$REPORT/install.log"
else
  echo "===== REUSE EXISTING GREEN ENV =====" | tee -a "$REPORT/install.log"
  [[ -x "$GREEN_ENV/bin/python" ]] || die "existing Green env has no Python: $GREEN_ENV"
  echo "Existing Green env will be reused after ROCm validation." | tee -a "$REPORT/install.log"
fi

PY="$GREEN_ENV/bin/python"

echo "===== GREEN BEFORE INSTALL =====" | tee -a "$REPORT/install.log"
"$PY" - <<'PY' | tee -a "$REPORT/install.log"
import sys, torch
print("python:", sys.executable)
print("torch:", torch.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available():
    raise SystemExit("[ERROR] Green accelerator unavailable")
if torch.version.hip is None:
    raise SystemExit("[ERROR] Green is not ROCm/HIP")
if "MI300" not in torch.cuda.get_device_name(0):
    raise SystemExit("[ERROR] unexpected Green GPU")
PY

"$PY" -m pip freeze > "$REPORT/green-before-pip-freeze.txt"

echo "===== PROBE OFFICIAL ROCm 6.4 INDEXES =====" | tee -a "$REPORT/install.log"
echo "SGLang index : $SGLANG_INDEX" | tee -a "$REPORT/install.log"
echo "Kernel index : $SGL_KERNEL_INDEX" | tee -a "$REPORT/install.log"

# Download first. This changes no installed packages and proves that the pinned
# versions actually exist in the official index before installation begins.
if ! "$PY" -m pip download \
  --no-deps \
  --dest "$WHEELHOUSE" \
  --index-url "$SGLANG_INDEX" \
  "sglang==$SGLANG_VERSION" \
  2>&1 | tee "$REPORT/download-sglang.log"; then
  echo
  echo "[ERROR] sglang==$SGLANG_VERSION is not downloadable from the official ROCm 6.4 index."
  echo "Inspect available versions with:"
  echo "  $PY -m pip index versions sglang --index-url '$SGLANG_INDEX'"
  exit 10
fi

if ! "$PY" -m pip download \
  --no-deps \
  --dest "$WHEELHOUSE" \
  --index-url "$SGL_KERNEL_INDEX" \
  "sgl-kernel==$SGL_KERNEL_VERSION" \
  2>&1 | tee "$REPORT/download-sgl-kernel.log"; then
  echo
  echo "[ERROR] sgl-kernel==$SGL_KERNEL_VERSION is not downloadable from the official ROCm 6.4 index."
  echo "Inspect available versions with:"
  echo "  $PY -m pip index versions sgl-kernel --index-url '$SGL_KERNEL_INDEX'"
  exit 11
fi

SGLANG_WHEEL="$(find "$WHEELHOUSE" -maxdepth 1 -type f -name 'sglang-*.whl' | head -1)"
KERNEL_WHEEL="$(find "$WHEELHOUSE" -maxdepth 1 -type f \( -name 'sgl_kernel-*.whl' -o -name 'sglang_kernel-*.whl' \) | head -1)"

[[ -n "$SGLANG_WHEEL" ]] || die "download succeeded but SGLang wheel was not found"
[[ -n "$KERNEL_WHEEL" ]] || die "download succeeded but kernel wheel was not found"

echo "Downloaded SGLang wheel: $SGLANG_WHEEL" | tee -a "$REPORT/install.log"
echo "Downloaded kernel wheel: $KERNEL_WHEEL" | tee -a "$REPORT/install.log"
sha256sum "$SGLANG_WHEEL" "$KERNEL_WHEEL" | tee "$REPORT/wheels.sha256"

echo "===== INSTALL ROCm KERNEL WHEEL (NO DEPENDENCIES) =====" | tee -a "$REPORT/install.log"
"$PY" -m pip install \
  --no-deps \
  --force-reinstall \
  "$KERNEL_WHEEL" \
  2>&1 | tee -a "$REPORT/install.log"

echo "===== DEPENDENCY DRY-RUN =====" | tee -a "$REPORT/install.log"
PLAN="$REPORT/pip-plan.json"

# Resolve normal Python dependencies from PyPI, but do not allow the plan to
# replace the already-working ROCm torch stack. No install occurs in this step.
"$PY" -m pip install \
  --dry-run \
  --report "$PLAN" \
  --upgrade-strategy only-if-needed \
  --index-url "$PYPI_INDEX" \
  "$SGLANG_WHEEL" \
  2>&1 | tee "$REPORT/pip-dry-run.log"

"$PY" - "$PLAN" <<'PY'
import json, sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text())
protected = {
    "torch",
    "torchvision",
    "torchaudio",
    "pytorch-triton-rocm",
    "triton",
}
planned = []
bad = []
for item in plan.get("install", []):
    meta = item.get("metadata") or {}
    name = str(meta.get("name") or "").lower().replace("_", "-")
    version = str(meta.get("version") or "")
    if name:
        planned.append((name, version))
    if name in protected:
        bad.append((name, version))

print("Planned packages:")
for name, version in planned:
    print(f"  {name}=={version}")

if bad:
    print("\n[REFUSE] Dependency plan would replace the ROCm torch/triton stack:")
    for name, version in bad:
        print(f"  {name}=={version}")
    raise SystemExit(20)
PY

echo "===== INSTALL SGLANG + SAFE DEPENDENCIES =====" | tee -a "$REPORT/install.log"
"$PY" -m pip install \
  --upgrade-strategy only-if-needed \
  --index-url "$PYPI_INDEX" \
  "$SGLANG_WHEEL" \
  2>&1 | tee -a "$REPORT/install.log"

# Re-assert the exact ROCm kernel candidate in case dependency resolution selected
# a different generic kernel build.
"$PY" -m pip install \
  --no-deps \
  --force-reinstall \
  "$KERNEL_WHEEL" \
  2>&1 | tee -a "$REPORT/install.log"

echo "===== VERIFY GREEN RUNTIME =====" | tee -a "$REPORT/install.log"
"$PY" - <<'PY' | tee -a "$REPORT/install.log"
import sys
import importlib.metadata as md
import torch
import transformers
import sglang

print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("sglang:", getattr(sglang, "__version__", md.version("sglang")))
for pkg in ("sgl-kernel", "sglang-kernel"):
    try:
        print(f"{pkg}:", md.version(pkg))
        break
    except md.PackageNotFoundError:
        pass

print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)

if not torch.cuda.is_available():
    raise SystemExit("[ERROR] Green has no accelerator")
if torch.version.hip is None:
    raise SystemExit("[ERROR] Green is not ROCm/HIP")
if "MI300" not in torch.cuda.get_device_name(0):
    raise SystemExit("[ERROR] unexpected GPU")
PY

"$PY" -m pip freeze > "$REPORT/green-after-pip-freeze.txt"
"$CONDA" list --prefix "$GREEN_ENV" > "$REPORT/green-conda-list.txt"

echo "===== PIP CHECK =====" | tee -a "$REPORT/install.log"
if ! "$PY" -m pip check 2>&1 | tee "$REPORT/pip-check.txt"; then
  echo "[ERROR] pip check failed; Green is not accepted." | tee -a "$REPORT/install.log"
  exit 30
fi

echo "===== SGLANG CHECK_ENV =====" | tee -a "$REPORT/install.log"
"$PY" -m sglang.check_env 2>&1 | tee "$REPORT/sglang-check-env.txt" || true

echo
echo "[OK] Isolated Green runtime installed."
echo "Green env : $GREEN_ENV"
echo "SGLang   : $SGLANG_VERSION"
echo "Kernel   : $SGL_KERNEL_VERSION"
echo "Report   : $REPORT"
echo
echo "Production Blue was not restarted or rerouted."
