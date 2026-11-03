#!/usr/bin/env bash
set -Eeuo pipefail

MODEL_PATH="${MODEL_PATH:-/var/lib/jenkins/tomod/taide-12b}"

echo "===== MODEL ====="
python3 - "$MODEL_PATH" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "config.json"
cfg = json.loads(p.read_text())
text = cfg.get("text_config") or {}
print("model_type:", cfg.get("model_type"))
print("architectures:", cfg.get("architectures"))
print("top_level.max_position_embeddings:", cfg.get("max_position_embeddings"))
print("text_config.model_type:", text.get("model_type"))
print("text_config.max_position_embeddings:", text.get("max_position_embeddings"))
print("text_config.vocab_size:", text.get("vocab_size"))
print("text_config.torch_dtype:", text.get("torch_dtype"))
PY

echo
echo "===== PYTHON PACKAGES ====="
python3 - <<'PY'
import importlib
mods = ["torch", "transformers", "vllm", "sglang"]
for name in mods:
    try:
        m = importlib.import_module(name)
        print(f"{name}: {getattr(m, '__version__', 'installed/version-unknown')}")
    except Exception as e:
        print(f"{name}: NOT IMPORTABLE ({type(e).__name__})")
PY

echo
echo "===== GPU ====="
if command -v amd-smi >/dev/null 2>&1; then
  amd-smi metric -g 0 -u -m || true
else
  echo "amd-smi not found"
fi

echo
echo "===== CURRENT LISTENERS ====="
ss -ltnp 2>/dev/null | grep -E ':(8000|8001|8002|8003|8010|8011|8100|8200)\b' || true
