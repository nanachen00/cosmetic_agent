#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm}"
PY="$GREEN_ENV/bin/python"
BASE="${BASE:-$ROOT/_third_party}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
SRC="$BASE/sglang-v0.5.0-probe-$STAMP"
REPO="https://github.com/sgl-project/sglang.git"
TAG="v0.5.0"

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
  die "Green :8100 is already listening; this probe expects it to be free"
fi
for p in 8004 8005; do
  if listener "$p"; then
    die "retired/stale :$p is unexpectedly active"
  fi
done

mkdir -p "$BASE"

echo "===== GREEN RUNTIME ====="
"$PY" - <<'PY'
import sys, torch
print("python:", sys.executable)
print("torch:", torch.__version__)
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
echo "===== VERIFY UPSTREAM TAG ====="
TAG_REF="$(git ls-remote --tags "$REPO" "refs/tags/$TAG" | awk '{print $1}' | head -1)"
[[ -n "$TAG_REF" ]] || die "upstream tag $TAG not found"
echo "tag=$TAG"
echo "remote_ref=$TAG_REF"

echo
echo "===== CLONE SOURCE (READ-ONLY PROBE COPY) ====="
git clone --depth 1 --branch "$TAG" "$REPO" "$SRC"
cd "$SRC"

HEAD="$(git rev-parse HEAD)"
echo "source_dir=$SRC"
echo "head=$HEAD"
echo "describe=$(git describe --tags --always --dirty)"

echo
echo "===== GEMMA 3 SUPPORT EVIDENCE ====="
GEMMA_HITS="$(grep -RIn --exclude-dir=.git \
  -E 'Gemma3ForConditionalGeneration|Gemma3ForCausalLM' \
  python/sglang 2>/dev/null | head -40 || true)"
if [[ -z "$GEMMA_HITS" ]]; then
  echo "[FAIL] No Gemma3 architecture references found in v0.5.0 source."
  exit 10
fi
printf '%s\n' "$GEMMA_HITS"

echo
echo "===== GEMMA MODEL FILES ====="
find python/sglang -type f \
  \( -iname '*gemma3*' -o -iname '*gemma_3*' \) \
  -print | sort || true

echo
echo "===== AMD / ROCm PYPROJECT ====="
PYPROJECT=""
for p in \
  python/pyproject_other.toml \
  python/pyproject_rocm.toml \
  python/pyproject.toml
do
  if [[ -f "$p" ]]; then
    echo "found: $p"
    if [[ -z "$PYPROJECT" && "$p" != "python/pyproject.toml" ]]; then
      PYPROJECT="$p"
    fi
  fi
done
[[ -n "$PYPROJECT" ]] || PYPROJECT="python/pyproject.toml"

"$PY" - "$PYPROJECT" <<'PY'
import sys, tomllib
from pathlib import Path

p = Path(sys.argv[1])
data = tomllib.loads(p.read_text())
proj = data.get("project", {})
print("selected_pyproject:", p)
print("requires_python:", proj.get("requires-python"))
print("base_dependencies:")
for dep in proj.get("dependencies", []):
    print(" ", dep)

opt = proj.get("optional-dependencies", {})
print("optional_dependency_groups:")
for name in sorted(opt):
    if "hip" in name.lower() or "amd" in name.lower() or name in {"all", "srt"}:
        print(f"  [{name}]")
        for dep in opt[name]:
            print("   ", dep)
PY

echo
echo "===== TRANSFORMERS / TORCH REQUIREMENTS ====="
grep -RIn --include='pyproject*.toml' --include='requirements*.txt' \
  -E '(^|["[:space:]])(torch|torchvision|torchaudio|transformers|triton)([<>=~!;"]|$)' \
  python 2>/dev/null | head -120 || true

echo
echo "===== ROCm KERNEL BUILD ENTRYPOINTS ====="
mapfile -t ROCM_SETUP < <(
  find . -type f -name 'setup_rocm.py' -not -path './.git/*' | sort
)

if [[ "${#ROCM_SETUP[@]}" -eq 0 ]]; then
  echo "[FAIL] no setup_rocm.py found"
  exit 11
fi

printf '%s\n' "${ROCM_SETUP[@]}"

echo
echo "===== gfx942 SUPPORT IN ROCm BUILD CODE ====="
GFX_HITS="$(grep -RIn \
  --include='setup_rocm.py' \
  --include='*.py' \
  -E 'gfx942|AMDGPU_TARGET|GPU_ARCH' \
  "${ROCM_SETUP[@]%/*}" 2>/dev/null | head -100 || true)"
printf '%s\n' "$GFX_HITS"

if ! grep -Rqs 'gfx942' "${ROCM_SETUP[@]}"; then
  echo "[WARN] setup_rocm.py does not literally contain gfx942; inspect target handling before build."
else
  echo "[PASS] gfx942 appears in ROCm setup code."
fi

echo
echo "===== SGLANG SERVER ENTRYPOINT ====="
for f in \
  python/sglang/launch_server.py \
  python/sglang/srt/entrypoints/http_server.py
do
  if [[ -f "$f" ]]; then
    echo "found: $f"
  fi
done

echo
echo "===== CURRENT GREEN INSTALLED PACKAGES (NO CHANGES MADE) ====="
"$PY" - <<'PY'
import importlib.util
for name in ("sglang", "sgl_kernel", "vllm", "aiter"):
    spec = importlib.util.find_spec(name)
    print(f"{name}: {'present' if spec else 'absent'}")
PY

echo
echo "===== RESULT ====="
echo "[PASS] Source probe completed."
echo "No pip/conda package was installed or removed."
echo "No service was started, stopped, or rerouted."
echo
echo "Probe source retained at:"
echo "  $SRC"
echo
echo "Next step: use this probe output to generate the exact ROCm 6.4 source-build installer."
