#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm}"
PY="$GREEN_ENV/bin/python"
BASE="${BASE:-$ROOT/_third_party}"
REF="${SGLANG_REF:-v0.5.1.post2}"
EXPECTED_HEAD="${EXPECTED_HEAD:-e3e97a120bf7caebc1fad10880782900209d9cb4}"
SRC="${SGLANG_SOURCE_DIR:-}"

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

if [[ -z "$SRC" ]]; then
  SRC="$(
    find "$BASE" -maxdepth 1 -mindepth 1 -type d \
      -name 'sglang-v0.5.1.post2-probe-*' \
      -printf '%T@ %p\n' 2>/dev/null \
      | sort -nr \
      | head -1 \
      | cut -d' ' -f2-
  )"
fi

[[ -n "$SRC" ]] || die "No existing v0.5.1.post2 probe source directory found under $BASE"
[[ -d "$SRC/.git" ]] || die "Not a Git source checkout: $SRC"

cd "$SRC"

HEAD="$(git rev-parse HEAD)"
DESC="$(git describe --tags --always --dirty)"

echo "===== SOURCE IDENTITY ====="
echo "source_dir=$SRC"
echo "head=$HEAD"
echo "describe=$DESC"

[[ "$HEAD" == "$EXPECTED_HEAD" ]] || die "unexpected source HEAD: $HEAD"
[[ "$DESC" == "$REF" ]] || die "source is dirty or not at $REF: $DESC"

echo
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
echo "===== GEMMA 3 SUPPORT (EXACT TAG) ====="
GEMMA_HITS="$(
  grep -RIn --exclude-dir=.git \
    -E 'Gemma3ForConditionalGeneration|Gemma3ForCausalLM' \
    python/sglang 2>/dev/null \
    | head -80 || true
)"
[[ -n "$GEMMA_HITS" ]] || die "Gemma 3 architecture support not found"
printf '%s\n' "$GEMMA_HITS"

echo
echo "===== PYPROJECT PARSE BACKEND ====="
"$PY" - <<'PY'
backend = None
try:
    import tomllib as toml
    backend = "stdlib tomllib"
except ModuleNotFoundError:
    try:
        import tomli as toml
        backend = "tomli"
    except ModuleNotFoundError:
        from pip._vendor import tomli as toml
        backend = "pip._vendor.tomli"
print(backend)
PY

echo
echo "===== PYPROJECT DEPENDENCIES / ROCm EXTRAS ====="
"$PY" - <<'PY'
from pathlib import Path

try:
    import tomllib as toml
except ModuleNotFoundError:
    try:
        import tomli as toml
    except ModuleNotFoundError:
        from pip._vendor import tomli as toml

p = Path("python/pyproject.toml")
if not p.is_file():
    raise SystemExit("[ERROR] python/pyproject.toml missing")

with p.open("rb") as f:
    data = toml.load(f)

proj = data.get("project", {})
print("file:", p)
print("requires-python:", proj.get("requires-python"))

print("\nbase dependencies:")
for dep in proj.get("dependencies", []):
    print(" ", dep)

extras = proj.get("optional-dependencies", {})
print("\noptional dependency groups:")
for name in sorted(extras):
    low = name.lower()
    if any(k in low for k in ("hip", "amd", "srt", "tracing", "all")):
        print(f"[{name}]")
        for dep in extras[name]:
            print(" ", dep)

interesting = ("torch", "torchvision", "torchaudio", "transformers", "triton",
               "vllm", "aiter", "sgl-kernel", "sgl_kernel")
print("\ninteresting dependency references:")
for section_name, deps in [("base", proj.get("dependencies", []))] + list(extras.items()):
    for dep in deps:
        low = dep.lower()
        if any(x in low for x in interesting):
            print(f"{section_name}: {dep}")
PY

echo
echo "===== RAW REQUIREMENT REFERENCES ====="
grep -RIn \
  --include='pyproject*.toml' \
  --include='requirements*.txt' \
  -E 'torch([<>=~!;"]|$)|torchvision|torchaudio|transformers|triton|vllm|aiter|sgl-kernel|sgl_kernel' \
  python sgl-kernel 2>/dev/null \
  | head -240 || true

echo
echo "===== ROCm BUILD ENTRYPOINTS ====="
mapfile -t ROCM_SETUPS < <(
  find . -type f -name 'setup_rocm.py' -not -path './.git/*' | sort
)
if [[ "${#ROCM_SETUPS[@]}" -eq 0 ]]; then
  die "No setup_rocm.py found"
fi
printf '%s\n' "${ROCM_SETUPS[@]}"

echo
echo "===== gfx942 / ARCH TARGET SUPPORT ====="
GFX_LITERAL=0
for f in "${ROCM_SETUPS[@]}"; do
  echo "--- $f ---"
  grep -nE 'gfx942|AMDGPU_TARGET|GPU_ARCH|PYTORCH_ROCM_ARCH|amdgpu_target' "$f" || true
  if grep -q 'gfx942' "$f"; then
    GFX_LITERAL=1
  fi
done
if [[ "$GFX_LITERAL" -eq 1 ]]; then
  echo "[PASS] gfx942 appears explicitly in ROCm setup code."
else
  echo "[WARN] gfx942 is not literal in setup_rocm.py; inspect dynamic arch detection."
fi

echo
echo "===== sgl-kernel METADATA ====="
if [[ -f "sgl-kernel/pyproject.toml" ]]; then
  "$PY" - <<'PY'
from pathlib import Path
try:
    import tomllib as toml
except ModuleNotFoundError:
    try:
        import tomli as toml
    except ModuleNotFoundError:
        from pip._vendor import tomli as toml

p = Path("sgl-kernel/pyproject.toml")
with p.open("rb") as f:
    d = toml.load(f)
proj = d.get("project", {})
print("name:", proj.get("name"))
print("version:", proj.get("version"))
print("requires-python:", proj.get("requires-python"))
for dep in proj.get("dependencies", []):
    print("dep:", dep)
PY
else
  echo "sgl-kernel/pyproject.toml not present"
fi

echo
echo "===== SERVER FLAGS IN EXACT TAG ====="
for f in \
  python/sglang/launch_server.py \
  python/sglang/srt/entrypoints/http_server.py \
  python/sglang/srt/server_args.py
do
  [[ -f "$f" ]] && echo "found: $f"
done

grep -RIn \
  --include='*.py' \
  -E 'mem-fraction-static|context-length|max-running-requests|disable-cuda-graph|attention-backend|served-model-name|enable-metrics' \
  python/sglang 2>/dev/null \
  | head -180 || true

echo
echo "===== AITER / vLLM IMPORTS IN RUNTIME PATH ====="
grep -RIn \
  --include='*.py' \
  -E '(^|[[:space:]])(from|import)[[:space:]]+(aiter|vllm)|SGLANG_USE_AITER' \
  python/sglang 2>/dev/null \
  | head -180 || true

echo
echo "===== CURRENT GREEN IMPORT STATE ====="
"$PY" - <<'PY'
import importlib.util
for name in ("sglang", "sgl_kernel", "vllm", "aiter", "tomli"):
    print(f"{name}: {'present' if importlib.util.find_spec(name) else 'absent'}")
PY

echo
echo "===== RESULT ====="
echo "[PASS] R2 source probe completed."
echo "No package was installed, removed, or upgraded."
echo "No service was started, stopped, or rerouted."
echo "Source checkout was reused without modification:"
echo "  $SRC"
