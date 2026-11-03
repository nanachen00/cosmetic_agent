#!/usr/bin/env bash
set -Eeuo pipefail

CONDA="${CONDA:-/opt/conda/bin/conda}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"

[[ "$GREEN_ENV" == "/opt/conda/envs/vllm-rocm-v092" ]] || {
  echo "[REFUSE] unexpected GREEN_ENV=$GREEN_ENV"
  exit 2
}

if ss -H -ltn "sport = :8200" 2>/dev/null | grep -q .; then
  echo "[REFUSE] :8200 is still listening. Stop vLLM first."
  exit 2
fi

if [[ ! -d "$GREEN_ENV" ]]; then
  echo "[OK] vLLM Green env does not exist."
  exit 0
fi

for proc in /proc/[0-9]*; do
  exe="$(readlink -f "$proc/exe" 2>/dev/null || true)"
  case "$exe" in
    "$GREEN_ENV"/*)
      echo "[REFUSE] live process uses vLLM env: PID=${proc##*/} EXE=$exe"
      exit 3
      ;;
  esac
done

"$CONDA" env remove -y --prefix "$GREEN_ENV"
echo "[OK] removed only $GREEN_ENV"
