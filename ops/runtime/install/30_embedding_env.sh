#!/usr/bin/env bash
# Verify the independent hash-locked BGE environment. Never mutate vLLM/photo.
# Create a NEW environment with install_photo_gpu_runtime.py --profile
# secure-rocm71 --python-lock requirements-bge-linux-py311.lock first.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../runtime.conf"
"$SCRIPT_DIR/../verify-mi300x-host.sh"
"$SCRIPT_DIR/../verify-mi300x-mount.sh" --check-only

PY="$EMBED_ENV/bin/python"
[[ -x "$PY" ]] || die "缺少獨立安全 BGE 環境：$PY；請先建立 hash-locked secure-rocm71 環境，不會修改既有 vLLM"
[[ "$(cd "$EMBED_ENV" && pwd -P)" != "$(cd "$VLLM_ENV" 2>/dev/null && pwd -P || true)" ]] \
  || die "BGE 不能使用歷史 vLLM 共用環境"
log "30. 只驗證獨立 BGE 環境 $EMBED_ENV；不安裝或降版套件"
env -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD -u LD_LIBRARY_PATH \
  PYTHONNOUSERSITE=1 BGE_SOURCE="$EMBEDDING_SERVICE_SOURCE" \
  "$PY" - <<'PY'
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

root = Path('/mnt/goodagents')
source = Path(os.environ['BGE_SOURCE']).resolve()
prefix = Path(sys.prefix).resolve()
if not source.is_relative_to(root) or not prefix.is_relative_to(root):
    raise SystemExit('BGE source and environment must be persistent')
required = {'bge_secure_service.py', 'bounded_http.py', 'bge-m3.secure.manifest.json', 'requirements-bge-linux-py311.lock'}
manifest = json.loads((source / 'source-manifest.json').read_text())
if not required.issubset(manifest):
    raise SystemExit('Incomplete immutable BGE source manifest')
for name, digest in manifest.items():
    path = source / name
    if not path.resolve().is_relative_to(source) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit('BGE source checksum differs')
lock = source / 'requirements-bge-linux-py311.lock'
ready = json.loads((prefix / 'runtime-ready.json').read_text())
if ready.get('profile') != 'secure-rocm71' or ready.get('python_lock_sha256') != hashlib.sha256(lock.read_bytes()).hexdigest():
    raise SystemExit('BGE secure runtime profile/hash lock differs')
expected = {'torch': '2.13.0+rocm7.1', 'torchvision': '0.28.0+rocm7.1', 'triton-rocm': '3.7.1'}
for line in lock.read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#'):
        name, version = line.split()[0].split('==')
        expected[name] = version
for name, version in expected.items():
    if importlib.metadata.version(name) != version:
        raise SystemExit(f'Installed BGE package differs from hash lock: {name}')
print('BGE_SECURE_ENV_VERIFIED', len(expected), 'packages; weights/API parity are separate validation gates')
PY
