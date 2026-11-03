#!/usr/bin/env bash
set -Eeuo pipefail

CONDA="${CONDA:-/opt/conda/bin/conda}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm-v051p2}"

[[ "$GREEN_ENV" == "/opt/conda/envs/sglang-rocm-v051p2" ]] || {
  echo "[REFUSE] unexpected GREEN_ENV=$GREEN_ENV"
  exit 2
}

if ss -H -ltn "sport = :8100" 2>/dev/null | grep -q .; then
  echo "[REFUSE] :8100 is still listening. Stop Green first."
  exit 2
fi

if [[ ! -d "$GREEN_ENV" ]]; then
  echo "[OK] R3 Green env does not exist."
  exit 0
fi

for proc in /proc/[0-9]*; do
  exe="$(readlink -f "$proc/exe" 2>/dev/null || true)"
  case "$exe" in
    "$GREEN_ENV"/*)
      echo "[REFUSE] live process uses Green env: PID=${proc##*/} EXE=$exe"
      exit 3
      ;;
  esac
done

"$CONDA" env remove -y --prefix "$GREEN_ENV"
echo "[OK] removed only $GREEN_ENV"
