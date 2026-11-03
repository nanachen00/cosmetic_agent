#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
MODEL="${SGLANG_MODEL_PATH:-$ROOT/taide-12b}"
PROD_PY=/opt/conda/envs/rocm_env/bin/python
GREEN_PY="${SGLANG_PYTHON:-/opt/conda/envs/sglang-rocm/bin/python}"
listener(){ ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }
echo '===== PORTS ====='
for p in 8000 8001 8002 8003 8004 8005 8010 8011 8100; do
  if listener "$p"; then echo ":$p LISTENING"; else echo ":$p DOWN/FREE"; fi
done
listener 8000 || { echo '[ERROR] Blue :8000 is not listening'; exit 2; }
! listener 8100 || { echo '[ERROR] Green :8100 already in use'; exit 2; }
for p in 8004 8005; do ! listener "$p" || { echo "[ERROR] retired/stale :$p active"; exit 2; }; done

echo; echo '===== MODEL ====='
python3 - "$MODEL" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]).resolve(); cfg=json.loads((p/'config.json').read_text()); t=cfg.get('text_config') or {}
print('path:',p); print('model_type:',cfg.get('model_type')); print('architectures:',cfg.get('architectures'))
print('text_config.model_type:',t.get('model_type')); print('text_config.max_position_embeddings:',t.get('max_position_embeddings')); print('text_config.vocab_size:',t.get('vocab_size'))
if cfg.get('architectures') != ['Gemma3ForConditionalGeneration']: raise SystemExit('[ERROR] unexpected architecture')
PY

echo; echo '===== PRODUCTION RUNTIME ====='
"$PROD_PY" - <<'PY'
import sys,torch,transformers
print('python:',sys.executable); print('torch:',torch.__version__); print('transformers:',transformers.__version__)
print('cuda_available:',torch.cuda.is_available()); print('cuda:',torch.version.cuda); print('hip:',torch.version.hip)
if torch.cuda.is_available(): print('device:',torch.cuda.get_device_name(0))
PY

echo; echo '===== GREEN RUNTIME ====='
echo "configured SGLANG_PYTHON=$GREEN_PY"
if [[ -x "$GREEN_PY" ]]; then
  if [[ "$(readlink -f "$GREEN_PY")" == "$(readlink -f "$PROD_PY")" ]]; then echo '[ERROR] Green must not reuse production rocm_env'; exit 2; fi
  "$GREEN_PY" "$(dirname "$0")/check_sglang_runtime.py"
else
  echo '[INFO] dedicated SGLang Python not installed at configured path yet'
fi

echo; echo '===== GPU ====='
command -v amd-smi >/dev/null 2>&1 && amd-smi metric -g 0 -u -m || true

echo; echo '===== CONTAINER RUNTIME ====='
for c in docker podman; do command -v "$c" >/dev/null 2>&1 && echo "$c: $(command -v "$c")" || echo "$c: not found"; done

echo '[OK] read-only preflight complete.'
