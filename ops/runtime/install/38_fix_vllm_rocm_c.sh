#!/usr/bin/env bash
# Fix for the vLLM v0.8.5 ROCm build bug seen in restore.log:
#   ImportError: vllm/_C.abi3.so: undefined symbol: _Z18cutlass_mla_decode...
# Cause: csrc/torch_bindings.cpp registers cutlass_mla_decode unconditionally, but its
# implementation (csrc/attention/mla/cutlass_mla_entry.cu) is only compiled for CUDA.
# Fix: wrap that registration in #ifndef USE_ROCM and rebuild the extension in place.
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"

PY="$VLLM_ENV/bin/python"
SRC="${VLLM_SOURCE_DIR:-$("$PY" -c 'import vllm,pathlib;print(pathlib.Path(vllm.__file__).resolve().parents[1])')}"
BINDINGS="$SRC/csrc/torch_bindings.cpp"
[[ -f "$BINDINGS" ]] || die "找不到 $BINDINGS（可設定 VLLM_SOURCE_DIR）"

if "$PY" -c "import vllm._C" 2>/dev/null; then
  echo "[OK] vllm._C 已可正常載入，不需修補"; exit 0
fi

log "38. 修補 $BINDINGS"
"$PY" - "$BINDINGS" <<'PY'
import re, sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
if "BLINK_ROCM_MLA_GUARD" in text:
    print("already patched"); sys.exit(0)
pattern = re.compile(
    r"(  // Compute MLA decode using cutlass\.\n.*?ops\.impl\(\"cutlass_mla_decode\", torch::kCUDA, &cutlass_mla_decode\);\n)",
    re.S,
)
new, count = pattern.subn(r"#ifndef USE_ROCM  // BLINK_ROCM_MLA_GUARD\n\1#endif\n", text)
if count != 1:
    sys.exit("cutlass_mla_decode registration block not found; patch manually")
path.with_suffix(".cpp.orig").write_text(text)
path.write_text(new)
print("patched (backup: torch_bindings.cpp.orig)")
PY

log "增量重新編譯 vLLM 擴充模組（只重編 torch_bindings 與連結，通常數分鐘）"
TORCH_LIB="$("$PY" -c 'import torch,pathlib;print(pathlib.Path(torch.__file__).parent/"lib")')"
export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export VLLM_TARGET_DEVICE=rocm PYTORCH_ROCM_ARCH=gfx942 GPU_ARCHS=gfx942 AMDGPU_TARGET=gfx942
export ROCM_PATH=/opt/rocm MAX_JOBS="${MAX_JOBS:-32}"
(cd "$SRC" && "$PY" setup.py develop --no-deps)

"$PY" - <<'PY'
import vllm._C, vllm._rocm_C, torch
for op in ("rms_norm", "fused_add_rms_norm", "silu_and_mul", "rotary_embedding"):
    assert hasattr(torch.ops._C, op), f"missing torch.ops._C.{op}"
print("[PASS] vllm._C / vllm._rocm_C 載入成功，核心運算已註冊")
PY
