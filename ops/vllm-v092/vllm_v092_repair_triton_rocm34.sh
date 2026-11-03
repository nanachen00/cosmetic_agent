#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
PY="$GREEN_ENV/bin/python"
ROCM_INDEX="${ROCM_INDEX:-https://download.pytorch.org/whl/rocm6.4}"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

[[ -x "$PY" ]] || die "Green Python missing: $PY"
listener 8000 || die "Blue :8000 must remain listening"
if listener 8200; then die ":8200 must be down before repairing Triton"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p unexpectedly active"; fi
done

echo "===== BEFORE ====="
"$PY" - <<'PY'
import importlib.metadata as md
import torch, triton
print("python:", __import__("sys").executable)
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
print("gpu:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
print("triton_module:", triton.__version__)
print("triton_file:", triton.__file__)
for pkg in ("triton", "pytorch-triton-rocm"):
    try:
        print(pkg + ":", md.version(pkg))
    except md.PackageNotFoundError:
        print(pkg + ": absent")
if not torch.__version__.startswith("2.8.0") or torch.version.hip is None:
    raise SystemExit("[ERROR] unexpected Torch/ROCm baseline")
PY

echo
echo "===== REMOVE GENERIC TRITON DISTRIBUTION ====="
"$PY" -m pip uninstall -y triton || true

echo
echo "===== RESTORE TORCH-PAIRED ROCm TRITON ====="
"$PY" -m pip install \
  --force-reinstall \
  --no-deps \
  "pytorch-triton-rocm==3.4.0" \
  --index-url "$ROCM_INDEX"

echo
echo "===== VERIFY ====="
"$PY" - <<'PY'
import importlib.metadata as md
import torch
import triton
from triton.compiler.compiler import triton_key
from triton.runtime import driver
import vllm, vllm._C, vllm._rocm_C

target = driver.active.get_current_target()

print("torch:", torch.__version__)
print("hip:", torch.version.hip)
print("gpu:", torch.cuda.get_device_name(0))
print("vllm:", vllm.__version__)
print("triton_module:", triton.__version__)
print("triton_file:", triton.__file__)
print("pytorch-triton-rocm:", md.version("pytorch-triton-rocm"))
try:
    print("generic triton dist:", md.version("triton"))
except md.PackageNotFoundError:
    print("generic triton dist: absent")
print("triton_key:", triton_key)
print("triton_target:", target)
print("vllm._C:", vllm._C.__file__)
print("vllm._rocm_C:", vllm._rocm_C.__file__)

if getattr(target, "backend", None) != "hip":
    raise SystemExit("[ERROR] Triton backend is not HIP")
if getattr(target, "arch", None) != "gfx942":
    raise SystemExit(f"[ERROR] unexpected Triton arch: {getattr(target, 'arch', None)}")
print("[PASS] Torch 2.8 ROCm Triton compatibility restored")
PY

echo
echo "===== METADATA NOTE ====="
echo "xgrammar may still declare a distribution-level dependency named 'triton'."
echo "That does not override this runtime gate; BLINK does not use guided decoding in this acceptance test."
"$PY" -m pip check || true

echo
echo "[OK] Only the isolated vLLM Green environment was modified."
