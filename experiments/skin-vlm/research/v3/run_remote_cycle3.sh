#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="$EXPERIMENT_DIR/.venv/bin/python"
DATA_DIR="$SCRIPT_DIR/records/data/frozen-v3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing experiment Python: $PYTHON_BIN" >&2
  exit 2
fi

cd "$EXPERIMENT_DIR"
PYTHONPYCACHEPREFIX=/tmp/blink-v3-pycache "$PYTHON_BIN" -m unittest -v research.v3.test_v3_research

cd "$SCRIPT_DIR"
"$PYTHON_BIN" prepare_cycle3_data.py \
  --raw ../cycles/v2/data/raw \
  --output "$DATA_DIR" \
  --exclude-manifest ../data/frozen-v1/train.jsonl \
  --exclude-manifest ../data/frozen-v1/val.jsonl \
  --exclude-manifest ../data/frozen-v1/test.jsonl \
  --exclude-manifest ../cycles/v2/data/frozen-v2/train.jsonl \
  --exclude-manifest ../cycles/v2/data/frozen-v2/val.jsonl \
  --exclude-manifest ../cycles/v2/data/frozen-v2/test.jsonl

"$PYTHON_BIN" gpu_monitor.py \
  --output records/gpu/cycle3-training.jsonl \
  --interval 2 \
  -- "$PYTHON_BIN" train_cycle3.py \
  --data "$DATA_DIR" \
  --model "$EXPERIMENT_DIR/model" \
  --output records/training

echo "Cycle 3 validation selection: $SCRIPT_DIR/records/training/selection.json"
echo "GPU summary: $SCRIPT_DIR/records/gpu/cycle3-training.summary.json"
