#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm-v051p2}"
PY="$GREEN_ENV/bin/python"
AITER_SRC="${AITER_SRC:-$ROOT/_third_party/aiter-v0.1.4}"
GPU_ARCH="${GPU_ARCH:-gfx942}"
MAX_JOBS="${MAX_JOBS:-4}"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

[[ -x "$PY" ]] || die "Green Python missing: $PY"
[[ -d "$AITER_SRC/.git" ]] || die "AITER source missing: $AITER_SRC"
listener 8000 || die "Blue :8000 must remain listening"
if listener 8100; then die "Green :8100 must remain free during recovery"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p is unexpectedly active"; fi
done

echo "===== GREEN RUNTIME ====="
"$PY" - <<'PY'
import sys, torch, transformers
print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] Green runtime is not ROCm/HIP capable")
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit(f"[ERROR] unexpected Torch version: {torch.__version__}")
if transformers.__version__ != "4.55.2":
    raise SystemExit(f"[ERROR] unexpected Transformers version: {transformers.__version__}")
PY

TORCH_LIB="$("$PY" - <<'PY'
from pathlib import Path
import torch
print(Path(torch.__file__).resolve().parent / "lib")
PY
)"

[[ -f "$TORCH_LIB/libc10.so" ]] || die "libc10.so missing from $TORCH_LIB"

export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export AITER_USE_SYSTEM_TRITON=1
export BUILD_TARGET=rocm
export GPU_ARCHS="$GPU_ARCH"
export MAX_JOBS

echo
echo "===== DYNAMIC LINKER PATH ====="
echo "TORCH_LIB=$TORCH_LIB"
echo "libc10=$TORCH_LIB/libc10.so"

cd "$AITER_SRC"

SO="$(find "$AITER_SRC/aiter" -type f -name 'aiter_*.so' -print 2>/dev/null | head -1 || true)"

if [[ -z "$SO" ]]; then
  echo
  echo "===== COMPILED AITER EXTENSION NOT FOUND; REBUILD ====="
  PREBUILD_KERNELS=1 "$PY" setup.py build_ext --inplace
  SO="$(find "$AITER_SRC/aiter" -type f -name 'aiter_*.so' -print 2>/dev/null | head -1 || true)"
fi

[[ -n "$SO" ]] || die "AITER compiled extension still not found after build"

echo
echo "===== COMPILED EXTENSION ====="
echo "$SO"

echo
echo "===== LDD BEFORE INSTALL ====="
if ldd "$SO" | tee /tmp/aiter-r3-ldd.txt | grep -q 'not found'; then
  echo "[ERROR] unresolved shared libraries remain:"
  grep 'not found' /tmp/aiter-r3-ldd.txt || true
  exit 3
fi
grep -E 'libc10|libtorch|libamd|libhip' /tmp/aiter-r3-ldd.txt || true

echo
echo "===== SOURCE IMPORT CHECK ====="
PYTHONPATH="$AITER_SRC${PYTHONPATH:+:$PYTHONPATH}" "$PY" - <<'PY'
import torch
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
from aiter.jit import core
print("aiter.jit.core:", core.__file__)
print("[PASS] source import")
PY

echo
echo "===== EDITABLE INSTALL (NO PEP517 BUILD ISOLATION) ====="
"$PY" -m pip install \
  --no-build-isolation \
  --config-settings editable_mode=compat \
  -e "$AITER_SRC"

echo
echo "===== INSTALLED IMPORT CHECK ====="
cd /tmp
"$PY" - <<'PY'
import importlib.metadata as md
import torch
import aiter
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
print("aiter_file:", aiter.__file__)
for name in ("amd-aiter", "aiter"):
    try:
        print("aiter_distribution:", name, md.version(name))
        break
    except md.PackageNotFoundError:
        pass
from aiter.jit import core
print("aiter.jit.core:", core.__file__)
print("[PASS] AITER editable install recovered")
PY

echo
echo "[OK] AITER recovery complete."
echo "Next: continue with the SGLang editable installation/final runtime verification."
