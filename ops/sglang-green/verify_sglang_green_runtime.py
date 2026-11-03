#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata as md
import json
import sys
from pathlib import Path

MODEL = Path("/var/lib/jenkins/tomod/taide-12b")

import torch
import transformers
import sglang

cfg = json.loads((MODEL / "config.json").read_text())
text_cfg = cfg.get("text_config") or {}

print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("sglang:", getattr(sglang, "__version__", md.version("sglang")))
try:
    print("sgl-kernel:", md.version("sgl-kernel"))
except Exception as exc:
    print("sgl-kernel: UNKNOWN", exc)

print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0))

print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("text_context:", text_cfg.get("max_position_embeddings"))

if not torch.cuda.is_available():
    raise SystemExit("[FAIL] accelerator unavailable")
if torch.version.hip is None:
    raise SystemExit("[FAIL] ROCm/HIP runtime required")
if cfg.get("architectures") != ["Gemma3ForConditionalGeneration"]:
    raise SystemExit("[FAIL] unexpected model architecture")

print("[PASS] Green runtime preflight")
