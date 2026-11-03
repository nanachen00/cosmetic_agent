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
if not torch.__version__.startswith("2.8.0"):
    raise SystemExit(f"[FAIL] unexpected Torch {torch.__version__}")
if transformers.__version__ != "4.53.2":
    raise SystemExit(f"[FAIL] unexpected Transformers {transformers.__version__}")

import vllm
import vllm._C
import vllm._rocm_C

print("vllm:", getattr(vllm, "__version__", md.version("vllm")))
print("vllm._C:", vllm._C.__file__)
print("vllm._rocm_C:", vllm._rocm_C.__file__)

import triton
print("triton_module:", triton.__version__)
print("triton_file:", triton.__file__)
for pkg in ("pytorch-triton-rocm", "triton"):
    try:
        print(pkg + ":", md.version(pkg))
    except md.PackageNotFoundError:
        print(pkg + ": absent")

try:
    from triton.runtime import driver
    target = driver.active.get_current_target()
    print("triton_target:", target)
except Exception as exc:
    print("[WARN] triton target probe:", type(exc).__name__, str(exc))

cfg = json.loads((MODEL / "config.json").read_text())
print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("text_context:", (cfg.get("text_config") or {}).get("max_position_embeddings"))
if cfg.get("architectures") != ["Gemma3ForConditionalGeneration"]:
    raise SystemExit("[FAIL] unexpected TAIDE architecture")

print("[PASS] vLLM v0.9.2 / Torch 2.8 ROCm runtime verification")
