#!/usr/bin/env python3
"""
Direct Hugging Face generation baseline for BLINK TAIDE.

This intentionally mirrors the current production loader in:
  /var/lib/jenkins/tomod/taide_server_graphrag.py
which uses AutoTokenizer + AutoModelForCausalLM + BF16 + device_map="auto".

Safety:
- By default, only prints model/config/runtime metadata.
- Actual model loading requires --yes-load-model because it creates another
  full model instance and consumes GPU HBM.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path


def load_cases(path: str | None):
    if not path:
        return [
            {
                "name": "smoke_ok",
                "messages": [{"role": "user", "content": "只回答 OK"}],
                "max_new_tokens": 16,
                "temperature": 0.0,
            }
        ]
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-path", default="/var/lib/jenkins/tomod/taide-12b")
    ap.add_argument("--cases", default=None)
    ap.add_argument("--yes-load-model", action="store_true")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    model_path = Path(args.model_path).resolve()
    config_path = model_path / "config.json"
    if not config_path.is_file():
        raise SystemExit(f"[ERROR] missing {config_path}")

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    text_cfg = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else {}

    print("===== MODEL CONFIG =====")
    print(f"path={model_path}")
    print(f"model_type={cfg.get('model_type')}")
    print(f"architectures={cfg.get('architectures')}")
    print(f"top_level_torch_dtype={cfg.get('torch_dtype')}")
    print(f"top_level_max_position_embeddings={cfg.get('max_position_embeddings')}")
    print(f"text_config.model_type={text_cfg.get('model_type')}")
    print(f"text_config.torch_dtype={text_cfg.get('torch_dtype')}")
    print(f"text_config.max_position_embeddings={text_cfg.get('max_position_embeddings')}")
    print(f"text_config.vocab_size={text_cfg.get('vocab_size')}")

    try:
        import torch
        import transformers
    except Exception as exc:
        raise SystemExit(f"[ERROR] runtime import failed: {type(exc).__name__}: {exc}")

    print("\n===== RUNTIME =====")
    print(f"python={platform.python_version()}")
    print(f"torch={torch.__version__}")
    print(f"transformers={transformers.__version__}")
    print(f"torch.cuda.is_available={torch.cuda.is_available()}")

    if not torch.cuda.is_available():
        raise SystemExit(
            "[ERROR] GPU accelerator unavailable; refusing CPU generation.\n"
            f"torch={torch.__version__}\n"
            f"cuda={torch.version.cuda}\n"
            f"hip={torch.version.hip}"
        )

    if torch.version.hip is None:
        raise SystemExit(
            "[ERROR] ROCm PyTorch required for this benchmark; "
            f"current torch={torch.__version__}"
        )
    
    if torch.cuda.is_available():
        try:
            print(f"device_name={torch.cuda.get_device_name(0)}")
        except Exception:
            pass


    if not args.yes_load_model:
        print("\n[SAFE EXIT] Metadata only. Add --yes-load-model to load another model copy and run generate().")
        return 0

    from transformers import AutoModelForCausalLM, AutoTokenizer

    cases = load_cases(args.cases)

    print("\n===== LOAD TOKENIZER =====")
    t0 = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
    tokenizer_load_s = time.perf_counter() - t0
    print(f"tokenizer_load_s={tokenizer_load_s:.3f}")

    print("\n===== LOAD MODEL =====")
    t0 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        str(model_path),
        torch_dtype=torch.bfloat16,
        device_map="auto",
        local_files_only=True,
    )
    model.eval()
    model_load_s = time.perf_counter() - t0
    print(f"model_class={model.__class__.__name__}")
    print(f"model_load_s={model_load_s:.3f}")
    print(f"model_device={getattr(model, 'device', 'unknown')}")

    results = []
    for case in cases:
        name = str(case["name"])
        messages = case["messages"]
        max_new_tokens = int(case.get("max_new_tokens", 64))
        temperature = float(case.get("temperature", 0.0))

        rendered = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = tokenizer(rendered, return_tensors="pt").to(model.device)

        kwargs = {
            "input_ids": inputs.input_ids,
            "attention_mask": inputs.attention_mask,
            "max_new_tokens": max_new_tokens,
            "do_sample": temperature > 0,
        }
        if temperature > 0:
            kwargs["temperature"] = temperature

        if torch.cuda.is_available():
            torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            outputs = model.generate(**kwargs)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - started

        prompt_tokens = int(inputs.input_ids.shape[1])
        completion_tokens = int(outputs.shape[1] - inputs.input_ids.shape[1])
        content = tokenizer.decode(
            outputs[0][inputs.input_ids.shape[1]:],
            skip_special_tokens=True,
        ).strip()

        row = {
            "name": name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "generation_s": round(elapsed, 4),
            "completion_tokens_per_s": round(completion_tokens / elapsed, 4) if elapsed > 0 else None,
            "content": content,
        }
        results.append(row)

        print(f"\n===== CASE {name} =====")
        print(json.dumps(row, ensure_ascii=False, indent=2))

    report = {
        "model_path": str(model_path),
        "model_class": model.__class__.__name__,
        "architecture": cfg.get("architectures"),
        "tokenizer_load_s": round(tokenizer_load_s, 4),
        "model_load_s": round(model_load_s, 4),
        "results": results,
    }

    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[OK] report={args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
