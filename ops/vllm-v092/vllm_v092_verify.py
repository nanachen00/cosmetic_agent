#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata as md
import json
import os
import sys
from pathlib import Path

MODEL = Path(os.environ.get("TAIDE_MODEL_PATH", "/var/lib/jenkins/tomod/taide-12b"))

import torch
import transformers

print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("available:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)

if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[FAIL] ROCm/HIP runtime unavailable")
if not torch.__version__.startswith("2.7.0"):
    raise SystemExit(f"[FAIL] unexpected Torch {torch.__version__}")

import vllm
print("vllm:", getattr(vllm, "__version__", md.version("vllm")))

try:
    import vllm._C
    print("vllm._C:", vllm._C.__file__)
except Exception as exc:
    raise SystemExit(f"[FAIL] vllm._C import: {type(exc).__name__}: {exc}")

cfg = json.loads((MODEL / "config.json").read_text())
print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("text_context:", (cfg.get("text_config") or {}).get("max_position_embeddings"))
if cfg.get("architectures") != ["Gemma3ForConditionalGeneration"]:
    raise SystemExit("[FAIL] unexpected TAIDE architecture")

try:
    from vllm.platforms import current_platform
    print("platform:", current_platform)
    print("platform_device_name:", current_platform.get_device_name())
except Exception as exc:
    print("[WARN] platform introspection:", type(exc).__name__, str(exc))

print("[PASS] vLLM v0.9.2 runtime verification")
