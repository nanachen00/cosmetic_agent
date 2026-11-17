#!/usr/bin/env bash
# Verify the already-compiled inference env (vLLM + SGLang in $VLLM_ENV) instead of rebuilding.
# Read-only legacy verification; never changes the shared restored environment.
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"
[[ "$ENABLE_TAIDE" == "1" ]] || die "vLLM 驗證需要明確指定 ENABLE_TAIDE=1"

log "40. 檢查已編譯的推論環境 ${VLLM_ENV}（不重新編譯）"
PY="$VLLM_ENV/bin/python"
[[ -x "$PY" ]] || die "找不到 ${PY}；若環境在別處，請設定 VLLM_ENV=<路徑> 再執行"

"$PY" -c "import httpx, huggingface_hub" 2>/dev/null \
  || die "舊 vLLM 環境缺少依賴；停止驗證，不自動 pip 修改共用環境"

"$PY" - <<'PY'
import importlib, torch
print("torch", torch.__version__, "| hip", torch.version.hip,
      "| gpu", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
assert torch.version.hip and torch.cuda.is_available(), "ROCm torch unavailable"
for name in ("vllm", "sglang", "sgl_kernel", "transformers", "httpx", "fastapi", "uvicorn"):
    try:
        mod = importlib.import_module(name)
        print(f"{name:12s} {getattr(mod, '__version__', 'OK')}")
    except Exception as exc:
        print(f"{name:12s} MISSING ({type(exc).__name__}: {exc})")
try:  # import directly: vllm._custom_ops only logs a warning when _C is broken
    import vllm._C, vllm._rocm_C  # noqa: F401
except ImportError as exc:
    raise SystemExit(f"[FAIL] vllm._C 無法載入：{exc}\n       請執行 install/38_fix_vllm_rocm_c.sh")
from transformers import AutoConfig  # Gemma 3 must be known to this transformers build
from transformers.models.gemma3 import configuration_gemma3  # noqa: F401
print("[PASS] vLLM ROCm kernels + Gemma 3 support")
PY
