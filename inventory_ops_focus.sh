#!/usr/bin/env bash
set -Eeuo pipefail

OPS="/var/lib/jenkins/tomod/ops"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="/var/lib/jenkins/tomod/ops-focus-${TS}.txt"

exec > >(tee "${OUT}") 2>&1

section() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

section "1. LISTENERS 8000-8300"

ss -ltnp 2>/dev/null \
    | awk '
        NR == 1 ||
        $4 ~ /:(8000|8001|8002|8003|8010|8100|8200|8300)$/
    ' || true


section "2. :8200 PROCESS"

PID="$(
    ss -ltnp 2>/dev/null \
    | sed -nE '/:8200[[:space:]]/s/.*pid=([0-9]+).*/\1/p' \
    | head -n1
)"

echo "PID=${PID:-NOT_FOUND}"

if [[ -n "${PID}" ]]; then

    echo
    echo "--- process"
    ps -p "${PID}" \
        -o pid,ppid,user,lstart,etime,%cpu,%mem,rss,vsz,args \
        || true

    echo
    echo "--- cwd"
    readlink -f "/proc/${PID}/cwd" || true

    echo
    echo "--- exe"
    readlink -f "/proc/${PID}/exe" || true

    echo
    echo "--- cmdline"
    tr '\0' ' ' < "/proc/${PID}/cmdline" || true
    echo

    echo
    echo "--- cgroup"
    cat "/proc/${PID}/cgroup" || true

    echo
    echo "--- selected environment"

    tr '\0' '\n' < "/proc/${PID}/environ" \
        | grep -E \
        '^(CONDA_PREFIX|VIRTUAL_ENV|PYTHONPATH|ROCM_PATH|HIP_VISIBLE_DEVICES|CUDA_VISIBLE_DEVICES|PYTORCH_ROCM_ARCH|HIP_FORCE_DEV_KERNARG|TORCH_BLAS_PREFER_HIPBLASLT|NCCL_[A-Z0-9_]+|RCCL_[A-Z0-9_]+|VLLM_[A-Z0-9_]+)=' \
        | grep -Evi \
        '(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|AUTH|COOKIE)' \
        | sort \
        || true

    echo
    echo "--- parent chain"

    P="${PID}"

    for _ in {1..12}; do
        [[ "${P}" =~ ^[0-9]+$ ]] || break
        [[ "${P}" -gt 0 ]] || break

        ps -p "${P}" \
            -o pid=,ppid=,user=,lstart=,args= \
            || break

        NEXT="$(
            ps -o ppid= -p "${P}" 2>/dev/null \
            | tr -d ' '
        )"

        [[ -n "${NEXT}" ]] || break
        [[ "${NEXT}" != "${P}" ]] || break

        P="${NEXT}"
    done
fi


section "3. SERVICE MANAGERS"

echo "--- systemd matching services"

systemctl list-units \
    --type=service \
    --all \
    --no-pager \
    2>/dev/null \
    | grep -Ei \
    'vllm|taide|blink|litellm|uvicorn|gunicorn|python|sglang' \
    || true

echo
echo "--- docker"

if command -v docker >/dev/null 2>&1; then
    docker ps --no-trunc \
        --format \
        'ID={{.ID}} IMAGE={{.Image}} PORTS={{.Ports}} NAMES={{.Names}} COMMAND={{.Command}}' \
        2>/dev/null || true
else
    echo "docker not installed"
fi

echo
echo "--- podman"

if command -v podman >/dev/null 2>&1; then
    podman ps --no-trunc 2>/dev/null || true
else
    echo "podman not installed"
fi


section "4. IMPORTANT OPS DIRECTORY CONTENT"

for D in \
    "${OPS}/vllm-v092" \
    "${OPS}/taide-engine-tests" \
    "${OPS}/runtime" \
    "${OPS}/staging" \
    "${OPS}/releases"
do
    echo
    echo "### ${D}"

    if [[ ! -e "${D}" ]]; then
        echo "MISSING"
        continue
    fi

    find "${D}" \
        -mindepth 1 \
        -maxdepth 3 \
        \( -type f -o -type l \) \
        ! -name '*.log' \
        ! -name '*.pyc' \
        -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %y %p -> %l\n' \
        2>/dev/null \
        | sort
done


section "5. OPS SYMBOLIC LINKS"

find "${OPS}" \
    -mindepth 1 \
    -maxdepth 4 \
    -type l \
    -printf '%p -> %l\n' \
    2>/dev/null \
    | sort


section "6. LIKELY SERVICE CONFIG / LAUNCHER FILES"

find \
    "${OPS}/vllm-v092" \
    "${OPS}/runtime" \
    "${OPS}/staging" \
    "${OPS}/releases" \
    -maxdepth 4 \
    -type f \
    \( \
        -name '*.sh' \
        -o -name '*.py' \
        -o -name '*.service' \
        -o -name '*.yaml' \
        -o -name '*.yml' \
        -o -name '*.toml' \
        -o -name '*.json' \
        -o -name '*.conf' \
    \) \
    ! -path '*/test-results/*' \
    ! -path '*/logs/*' \
    ! -path '*/__pycache__/*' \
    -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' \
    2>/dev/null \
    | sort


section "7. PYTHON / VLLM PROCESSES"

ps -eo pid,ppid,user,lstart,etime,%cpu,%mem,args \
    | grep -Ei \
    '[v]llm|[t]aide|[l]itellm|[s]glang|[u]vicorn|[g]unicorn' \
    || true


section "8. GPU PROCESS SNAPSHOT"

if command -v amd-smi >/dev/null 2>&1; then
    amd-smi process 2>/dev/null || true
elif command -v rocm-smi >/dev/null 2>&1; then
    rocm-smi --showpids 2>/dev/null || true
else
    echo "No amd-smi or rocm-smi"
fi


section "9. CURRENT NUMA / GPU VISIBILITY"

printf "numa_balancing="
cat /proc/sys/kernel/numa_balancing 2>/dev/null || true

echo

if command -v rocm-smi >/dev/null 2>&1; then
    rocm-smi --showproductname --showmeminfo vram \
        2>/dev/null || true
fi


section "DONE"

echo "${OUT}"
