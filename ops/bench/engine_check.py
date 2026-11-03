#!/usr/bin/env python3
"""Verify that the compiled vLLM and SGLang builds are usable on this MI300X.

Added 2026-10-22. Run with the inference env:  /opt/venv-llm/bin/python engine_check.py
Checks (each PASS / FAIL / WARN):
  - torch sees a ROCm GPU
  - vllm._C / vllm._rocm_C load (no undefined symbols)
  - vLLM custom kernels run on the GPU and match a PyTorch reference (rms_norm, silu_and_mul)
  - vLLM model registry knows Gemma3ForConditionalGeneration (TAIDE 12B)
  - sglang / sgl_kernel import, Gemma 3 model module present, sgl_kernel ops registered
Exit code 0 = everything required for serving passed.
"""

from __future__ import annotations

import importlib
import json
import sys
import traceback

RESULTS = []


def record(name, status, detail=""):
    RESULTS.append({"check": name, "status": status, "detail": str(detail)[:300]})
    print(f"[{status:4s}] {name}  {detail}", flush=True)


def check(name, fn, required=True):
    try:
        detail = fn()
        record(name, "PASS", detail or "")
        return True
    except Exception as exc:
        record(name, "FAIL" if required else "WARN", f"{type(exc).__name__}: {exc}")
        if "-v" in sys.argv:
            traceback.print_exc()
        return False


def torch_gpu():
    import torch
    assert torch.version.hip, "torch is not a ROCm build"
    assert torch.cuda.is_available(), "no GPU visible"
    return f"torch {torch.__version__} | HIP {torch.version.hip} | {torch.cuda.get_device_name(0)}"


def vllm_extensions():
    import vllm
    import vllm._C  # noqa: F401
    import vllm._rocm_C  # noqa: F401
    return f"vllm {vllm.__version__}"


def vllm_kernels():
    import torch
    torch.manual_seed(0)
    dev, dtype = "cuda", torch.bfloat16
    x = torch.randn(64, 3840, device=dev, dtype=dtype)          # Gemma 3 12B hidden size
    w = torch.randn(3840, device=dev, dtype=dtype)
    out = torch.empty_like(x)
    torch.ops._C.rms_norm(out, x, w, 1e-6)
    ref = (x.float() * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + 1e-6) * w.float()).to(dtype)
    err1 = (out.float() - ref.float()).abs().max().item()
    assert err1 < 0.1, f"rms_norm max error {err1}"

    gate_up = torch.randn(64, 2 * 15360, device=dev, dtype=dtype)
    out2 = torch.empty(64, 15360, device=dev, dtype=dtype)
    torch.ops._C.silu_and_mul(out2, gate_up)
    g, u = gate_up.float().chunk(2, dim=-1)
    ref2 = (torch.nn.functional.silu(g) * u).to(dtype)
    err2 = (out2.float() - ref2.float()).abs().max().item()
    assert err2 < 0.25, f"silu_and_mul max error {err2}"
    torch.cuda.synchronize()
    return f"rms_norm err={err1:.4f}, silu_and_mul err={err2:.4f}"


def vllm_gemma3():
    from vllm.model_executor.models import ModelRegistry
    archs = set(ModelRegistry.get_supported_archs())
    assert "Gemma3ForConditionalGeneration" in archs, "Gemma3ForConditionalGeneration not registered"
    return "Gemma3ForConditionalGeneration registered"


def sglang_import():
    import sglang
    import sglang.srt.server_args  # noqa: F401
    return f"sglang {sglang.__version__}"


def sgl_kernel_import():
    import sgl_kernel
    import torch
    ops = sorted(name for name in dir(torch.ops.sgl_kernel) if not name.startswith("_"))
    assert ops, "no torch.ops.sgl_kernel ops registered"
    return f"sgl_kernel {getattr(sgl_kernel, '__version__', '?')} | {len(ops)} ops (e.g. {', '.join(ops[:5])})"


def sglang_gemma3():
    for module in ("sglang.srt.models.gemma3_mm", "sglang.srt.models.gemma3_causal"):
        importlib.import_module(module)
    return "gemma3_mm + gemma3_causal importable"


def sglang_attention_backends():
    from sglang.srt.server_args import ServerArgs
    import inspect
    src = inspect.getsource(ServerArgs)
    found = [b for b in ("triton", "aiter", "torch_native", "flashinfer") if f'"{b}"' in src]
    return "attention backends known: " + ", ".join(found)


def main():
    ok = check("torch ROCm GPU", torch_gpu)
    if ok:
        check("vLLM compiled extensions load", vllm_extensions) and \
            check("vLLM kernels on GPU vs reference", vllm_kernels)
        check("vLLM supports Gemma 3 (TAIDE 12B)", vllm_gemma3)
        check("SGLang import", sglang_import) and \
            check("sgl_kernel ops registered", sgl_kernel_import) and \
            check("SGLang Gemma 3 model modules", sglang_gemma3)
        check("SGLang attention backends", sglang_attention_backends, required=False)
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (" — see FAIL lines above" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
