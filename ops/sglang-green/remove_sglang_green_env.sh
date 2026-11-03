#!/usr/bin/env bash
set -Eeuo pipefail

GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm}"
CONDA="${CONDA:-/opt/conda/bin/conda}"

[[ "$GREEN_ENV" == /opt/conda/envs/sglang-rocm ]] || {
  echo "[REFUSE] unexpected GREEN_ENV=$GREEN_ENV"
  exit 2
}

if ss -H -ltn "sport = :8100" 2>/dev/null | grep -q .; then
  echo "[REFUSE] :8100 is still listening. Stop Green first."
  exit 2
fi

if [[ ! -d "$GREEN_ENV" ]]; then
  echo "[OK] Green environment does not exist."
  exit 0
fi

# Refuse removal if a live process executable resides inside the Green environment.
FOUND=0
for proc in /proc/[0-9]*; do
  exe="$(readlink -f "$proc/exe" 2>/dev/null || true)"
  case "$exe" in
    "$GREEN_ENV"/*)
      echo "[REFUSE] live process uses Green env: PID=${proc##*/} EXE=$exe"
      FOUND=1
      ;;
  esac
done
[[ "$FOUND" -eq 0 ]] || exit 3

"$CONDA" env remove -y --prefix "$GREEN_ENV"

echo "[OK] Removed only isolated Green environment: $GREEN_ENV"
echo "Production /opt/conda/envs/rocm_env was not modified."
