#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm}"
PY="$GREEN_ENV/bin/python"
BASE="${BASE:-$ROOT/_third_party}"
REPO="${SGLANG_REPO:-https://github.com/sgl-project/sglang.git}"
REF="${SGLANG_REF:-v0.5.1.post2}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
SRC="$BASE/sglang-${REF}-probe-$STAMP"

die() {
  echo "[ERROR] $*" >&2
  exit 2
}

listener() {
  ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .
}

[[ -x "$PY" ]] || die "Green Python missing: $PY"
listener 8000 || die "Blue :8000 must remain listening"
if listener 8100; then
  die "Green :8100 must be free for this source-only probe"
fi
for port in 8004 8005; do
  if listener "$port"; then
    die "retired/stale :$port is unexpectedly active"
  fi
done

mkdir -p "$BASE"

echo "===== GREEN RUNTIME ====="
"$PY" - <<'PY'
import sys, torch, transformers
print("python:", sys.executable)
print("torch:", torch.__version__)
print("transformers:", transformers.__version__)
print("cuda_available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("hip:", torch.version.hip)
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] Green runtime is not ROCm/HIP capable")
if "MI300" not in torch.cuda.get_device_name(0):
    raise SystemExit("[ERROR] unexpected GPU")
PY

echo
echo "===== VERIFY UPSTREAM REF ====="
TAG_SHA="$(git ls-remote --refs --tags "$REPO" "refs/tags/$REF" | awk '{print $1}' | head -1)"
HEAD_SHA="$(git ls-remote --refs --heads "$REPO" "refs/heads/$REF" | awk '{print $1}' | head -1)"

if [[ -n "$TAG_SHA" ]]; then
  echo "ref_type=tag"
  echo "ref=$REF"
  echo "remote_sha=$TAG_SHA"
elif [[ -n "$HEAD_SHA" ]]; then
  echo "ref_type=branch"
  echo "ref=$REF"
  echo "remote_sha=$HEAD_SHA"
else
  echo "[ERROR] Neither refs/tags/$REF nor refs/heads/$REF exists upstream."
  echo
  echo "Nearby 0.5.1 refs:"
  git ls-remote --refs --tags "$REPO" \
    | awk '{print $2}' \
    | grep -E 'refs/tags/v0\.5\.1' \
    | tail -30 || true
  exit 3
fi

echo
echo "===== CLONE SOURCE ====="
git clone --depth 1 --branch "$REF" "$REPO" "$SRC"
cd "$SRC"

echo "source_dir=$SRC"
echo "head=$(git rev-parse HEAD)"
echo "describe=$(git describe --tags --always --dirty)"

echo
echo "===== GEMMA 3 SUPPORT ====="
GEMMA_HITS="$(grep -RIn --exclude-dir=.git \
  -E 'Gemma3ForConditionalGeneration|Gemma3ForCausalLM|Gemma3' \
  python/sglang 2>/dev/null | head -80 || true)"
if [[ -z "$GEMMA_HITS" ]]; then
  echo "[FAIL] No Gemma 3 references found under python/sglang."
  exit 10
fi
printf '%s\n' "$GEMMA_HITS"

echo
echo "===== GEMMA-RELATED SOURCE FILES ====="
find python/sglang -type f \
  \( -iname '*gemma3*' -o -iname '*gemma_3*' -o -iname '*gemma*' \) \
  -print | sort | head -80 || true

echo
echo "===== PYPROJECT FILES ====="
find python -maxdepth 2 -type f \
  \( -name 'pyproject*.toml' -o -name 'requirements*.txt' \) \
  -print | sort

echo
echo "===== PYPROJECT DEPENDENCIES / EXTRAS ====="
"$PY" - <<'PY'
import tomllib
from pathlib import Path

paths = sorted(Path("python").glob("pyproject*.toml"))
for p in paths:
    print(f"\n--- {p} ---")
    data = tomllib.loads(p.read_text())
    proj = data.get("project", {})
    print("requires-python:", proj.get("requires-python"))
    for dep in proj.get("dependencies", []):
        print("dep:", dep)
    extras = proj.get("optional-dependencies", {})
    for name, deps in extras.items():
        if any(k in name.lower() for k in ("hip", "amd", "srt", "all")):
            print(f"extra[{name}]:")
            for dep in deps:
                print(" ", dep)
PY

echo
echo "===== TORCH / TRANSFORMERS / TRITON / VLLM / AITER REFERENCES ====="
grep -RIn \
  --include='pyproject*.toml' \
  --include='requirements*.txt' \
  --include='*.py' \
  -E 'torch([<>=~!;"]|$)|transformers([<>=~!;"]|$)|triton([<>=~!;"]|$)|vllm|aiter' \
  python/sglang python/pyproject*.toml sgl-kernel 2>/dev/null \
  | head -180 || true

echo
echo "===== ROCm BUILD ENTRYPOINTS ====="
mapfile -t ROCM_SETUPS < <(
  find . -type f -name 'setup_rocm.py' -not -path './.git/*' | sort
)

if [[ "${#ROCM_SETUPS[@]}" -eq 0 ]]; then
  echo "[FAIL] No setup_rocm.py found."
  exit 11
fi

printf '%s\n' "${ROCM_SETUPS[@]}"

echo
echo "===== gfx942 / AMDGPU TARGET SUPPORT ====="
GFX_FOUND=0
for f in "${ROCM_SETUPS[@]}"; do
  echo "--- $f ---"
  grep -nE 'gfx942|AMDGPU_TARGET|GPU_ARCH|PYTORCH_ROCM_ARCH' "$f" || true
  if grep -q 'gfx942' "$f"; then
    GFX_FOUND=1
  fi
done

if [[ "$GFX_FOUND" -eq 1 ]]; then
  echo "[PASS] gfx942 appears explicitly in at least one ROCm setup script."
else
  echo "[WARN] gfx942 is not literal in setup_rocm.py; build may rely on environment detection."
fi

echo
echo "===== SERVER ENTRYPOINTS / FLAGS ====="
for f in \
  python/sglang/launch_server.py \
  python/sglang/srt/entrypoints/http_server.py \
  python/sglang/srt/server_args.py
do
  if [[ -f "$f" ]]; then
    echo "found: $f"
  fi
done

grep -RIn \
  --include='*.py' \
  -E 'mem-fraction-static|context-length|max-running-requests|disable-cuda-graph|attention-backend|served-model-name' \
  python/sglang 2>/dev/null | head -120 || true

echo
echo "===== CURRENT GREEN IMPORT STATE ====="
"$PY" - <<'PY'
import importlib.util
for name in ("sglang", "sgl_kernel", "vllm", "aiter"):
    print(f"{name}: {'present' if importlib.util.find_spec(name) else 'absent'}")
PY

echo
echo "===== RESULT ====="
echo "[PASS] Source probe completed."
echo "No pip/conda package was installed or removed."
echo "No service was started, stopped, or rerouted."
echo "Source retained at:"
echo "  $SRC"
