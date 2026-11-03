#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata as md
import importlib.util
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SGLANG_USE_AITER", "1")

MODEL = Path("/var/lib/jenkins/tomod/taide-12b")

import torch
import transformers

print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)

if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[FAIL] ROCm/HIP runtime unavailable")
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit(f"[FAIL] unexpected torch version {torch.__version__}")
if transformers.__version__ != "4.55.2":
    raise SystemExit(f"[FAIL] unexpected transformers version {transformers.__version__}")

import sglang
print("sglang:", getattr(sglang, "__version__", md.version("sglang")))
print("sgl-kernel:", md.version("sgl-kernel"))

aiter_spec = importlib.util.find_spec("aiter")
vllm_spec = importlib.util.find_spec("vllm")
print("aiter:", "present" if aiter_spec else "absent")
print("vllm:", "present" if vllm_spec else "absent")

if not aiter_spec:
    raise SystemExit("[FAIL] AITER missing")
if vllm_spec:
    print("[WARN] vLLM is installed in this Green environment; R3 expected BUILD_VLLM=0 path")

cfg = json.loads((MODEL / "config.json").read_text())
print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("text_context:", (cfg.get("text_config") or {}).get("max_position_embeddings"))

if cfg.get("architectures") != ["Gemma3ForConditionalGeneration"]:
    raise SystemExit("[FAIL] unexpected TAIDE architecture")

# Import modules that use the AITER/vLLM branching paths. These imports are a
# pre-launch contract check: with SGLANG_USE_AITER=1 they should not require vLLM.
checks = [
    "sglang.srt.layers.layernorm",
    "sglang.srt.layers.rotary_embedding",
    "sglang.srt.layers.quantization.fp8_kernel",
]
for name in checks:
    __import__(name)
    print("import_ok:", name)

print("[PASS] R3 runtime verification")
