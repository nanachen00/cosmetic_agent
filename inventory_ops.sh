#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod/ops}"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="/var/lib/jenkins/tomod/ops-inventory-${TS}.txt"

if [[ ! -d "${ROOT}" ]]; then
    echo "ERROR: directory does not exist: ${ROOT}" >&2
    exit 1
fi

exec > >(tee "${OUT}") 2>&1

echo "============================================================"
echo "OPS INVENTORY"
echo "============================================================"
echo "Generated : $(date --iso-8601=seconds)"
echo "Root      : ${ROOT}"
echo "Host      : $(hostname)"
echo
echo "READ-ONLY INVENTORY"
echo "No files are deleted, moved, modified, or hashed."
echo

echo
echo "============================================================"
echo "1. ROOT DIRECTORY"
echo "============================================================"

ls -lah \
    --time-style=long-iso \
    "${ROOT}"

echo
echo
echo "============================================================"
echo "2. TOP-LEVEL DISK USAGE"
echo "============================================================"
echo

du -x -B1 -d1 "${ROOT}" 2>/dev/null \
    | sort -nr \
    | awk '
        function human(x) {
            split("B KiB MiB GiB TiB PiB", u, " ");
            i=1;
            while (x >= 1024 && i < 6) {
                x /= 1024;
                i++;
            }
            return sprintf("%.2f %s", x, u[i]);
        }
        {
            bytes=$1;
            $1="";
            sub(/^ /, "", $0);
            printf "%12s  %s\n", human(bytes), $0;
        }
    '

echo
echo
echo "============================================================"
echo "3. DIRECTORY TREE - DEPTH <= 3"
echo "============================================================"
echo

ROOT="${ROOT}" python3 <<'PY'
import os
from pathlib import Path
from datetime import datetime

root = Path(os.environ["ROOT"]).resolve()
max_depth = 3

def mtime_text(path):
    try:
        ts = path.lstat().st_mtime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "UNKNOWN"

print(root)

for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
    current_path = Path(current)

    try:
        rel = current_path.relative_to(root)
        depth = len(rel.parts)
    except ValueError:
        continue

    dirs[:] = sorted(dirs)

    if depth >= max_depth:
        dirs[:] = []
        continue

    for dirname in dirs:
        p = current_path / dirname

        try:
            relp = p.relative_to(root)
        except ValueError:
            continue

        indent = "    " * len(relp.parts)

        suffix = ""
        if p.is_symlink():
            try:
                suffix = f" -> {os.readlink(p)}"
            except OSError:
                suffix = " -> [unreadable]"

        print(
            f"{indent}[DIR] {relp} "
            f"(mtime={mtime_text(p)}){suffix}"
        )
PY

echo
echo
echo "============================================================"
echo "4. TOP-LEVEL DIRECTORY STATISTICS"
echo "============================================================"
echo

printf "%-48s %12s %12s %20s\n" \
    "NAME" \
    "FILES" \
    "DIRS" \
    "LATEST_MTIME"

printf "%-48s %12s %12s %20s\n" \
    "------------------------------------------------" \
    "------------" \
    "------------" \
    "--------------------"

while IFS= read -r -d '' d; do
    name="$(basename "${d}")"

    files="$(
        find "${d}" -xdev -type f 2>/dev/null |
        wc -l
    )"

    dirs="$(
        find "${d}" -xdev -mindepth 1 -type d 2>/dev/null |
        wc -l
    )"

    latest="$(
        find "${d}" -xdev \
            -printf '%T@\n' 2>/dev/null |
        sort -nr |
        head -n1
    )"

    if [[ -n "${latest}" ]]; then
        latest_fmt="$(
            date -d "@${latest%.*}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
            || echo UNKNOWN
        )"
    else
        latest_fmt="UNKNOWN"
    fi

    printf "%-48s %12s %12s %20s\n" \
        "${name}" \
        "${files}" \
        "${dirs}" \
        "${latest_fmt}"

done < <(
    find "${ROOT}" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -print0 |
    sort -z
)

echo
echo
echo "============================================================"
echo "5. LARGE FILES >= 500 MiB"
echo "============================================================"
echo

find "${ROOT}" \
    -xdev \
    -type f \
    -size +500M \
    -printf '%s\t%TY-%Tm-%Td %TH:%TM:%TS\t%p\n' \
    2>/dev/null \
    | sort -nr \
    | head -n 100 \
    | awk -F '\t' '
        function human(x) {
            split("B KiB MiB GiB TiB PiB", u, " ");
            i=1;
            while (x >= 1024 && i < 6) {
                x /= 1024;
                i++;
            }
            return sprintf("%.2f %s", x, u[i]);
        }
        {
            printf "%12s  %-26s  %s\n", human($1), $2, $3
        }
    '

echo
echo
echo "============================================================"
echo "6. EXPERIMENT / RESULT / HANDOFF / ARCHIVE CANDIDATES"
echo "============================================================"
echo

find "${ROOT}" \
    -xdev \
    -mindepth 1 \
    -maxdepth 4 \
    -type d \
    \( \
        -iname '*result*' \
        -o -iname '*run*' \
        -o -iname '*experiment*' \
        -o -iname '*benchmark*' \
        -o -iname '*handoff*' \
        -o -iname '*archive*' \
        -o -iname '*backup*' \
        -o -iname '*old*' \
        -o -iname '*gate*' \
        -o -iname '*evidence*' \
        -o -iname '*diagnostic*' \
        -o -iname '*bundle*' \
        -o -iname '*tmp*' \
        -o -iname '*temp*' \
    \) \
    -printf '%TY-%Tm-%Td %TH:%TM\t%p\n' \
    2>/dev/null \
    | sort

echo
echo
echo "============================================================"
echo "7. ARCHIVE / BUNDLE FILES"
echo "============================================================"
echo

find "${ROOT}" \
    -xdev \
    -type f \
    \( \
        -iname '*.tar' \
        -o -iname '*.tar.gz' \
        -o -iname '*.tgz' \
        -o -iname '*.zip' \
        -o -iname '*.7z' \
    \) \
    -printf '%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' \
    2>/dev/null \
    | sort -nr \
    | awk -F '\t' '
        function human(x) {
            split("B KiB MiB GiB TiB PiB", u, " ");
            i=1;
            while (x >= 1024 && i < 6) {
                x /= 1024;
                i++;
            }
            return sprintf("%.2f %s", x, u[i]);
        }
        {
            printf "%12s  %-18s  %s\n", human($1), $2, $3
        }
    '

echo
echo
echo "============================================================"
echo "8. GIT REPOSITORIES"
echo "============================================================"
echo

while IFS= read -r gitdir; do
    repo="${gitdir%/.git}"

    echo
    echo "--- ${repo}"

    git -C "${repo}" rev-parse --show-toplevel 2>/dev/null || true

    printf "HEAD: "
    git -C "${repo}" rev-parse HEAD 2>/dev/null || echo "UNKNOWN"

    printf "BRANCH: "
    git -C "${repo}" branch --show-current 2>/dev/null || true

    echo "STATUS:"
    git -C "${repo}" status --short 2>/dev/null | head -n 40 || true

done < <(
    find "${ROOT}" \
        -xdev \
        -mindepth 2 \
        -maxdepth 5 \
        -type d \
        -name .git \
        2>/dev/null |
    sort
)

echo
echo
echo "============================================================"
echo "9. PROCESSES WHOSE CWD IS UNDER OPS"
echo "============================================================"
echo

found=0

for proc in /proc/[0-9]*; do
    pid="${proc##*/}"

    cwd="$(readlink -f "${proc}/cwd" 2>/dev/null || true)"

    [[ -n "${cwd}" ]] || continue

    case "${cwd}" in
        "${ROOT}"|"${ROOT}"/*)
            found=1

            cmd="$(
                tr '\0' ' ' < "${proc}/cmdline" 2>/dev/null \
                | sed 's/[[:space:]]\+/ /g' \
                || true
            )"

            printf "PID=%s\n" "${pid}"
            printf "CWD=%s\n" "${cwd}"
            printf "CMD=%s\n" "${cmd}"
            echo

            ;;
    esac
done

if [[ "${found}" == "0" ]]; then
    echo "No readable process CWD currently points into ${ROOT}"
fi

echo
echo
echo "============================================================"
echo "10. CURRENT :8200 LISTENER - REFERENCE ONLY"
echo "============================================================"
echo

ss -ltnp 2>/dev/null |
    grep -E '(^|[[:space:]])[^[:space:]]*:8200[[:space:]]' \
    || echo "No :8200 listener found or process information is unavailable."

PID="$(
    ss -ltnp 2>/dev/null |
    sed -nE \
        '/:8200[[:space:]]/s/.*pid=([0-9]+).*/\1/p' |
    head -n1
)"

if [[ -n "${PID}" ]]; then
    echo
    echo "PID=${PID}"

    printf "CWD="
    readlink -f "/proc/${PID}/cwd" 2>/dev/null || echo "UNKNOWN"

    printf "EXE="
    readlink -f "/proc/${PID}/exe" 2>/dev/null || echo "UNKNOWN"

    printf "CMD="
    tr '\0' ' ' < "/proc/${PID}/cmdline" 2>/dev/null || true
    echo

    printf "PPID="
    ps -o ppid= -p "${PID}" 2>/dev/null \
        | tr -d ' ' \
        || true
    echo

    echo
    echo "PROCESS:"
    ps -p "${PID}" \
        -o pid,ppid,user,lstart,etime,%cpu,%mem,rss,vsz,args \
        2>/dev/null || true

    echo
    echo "CGROUP:"
    cat "/proc/${PID}/cgroup" 2>/dev/null || true
fi

echo
echo
echo "============================================================"
echo "11. RECENT TOP-LEVEL CHANGES"
echo "============================================================"
echo

find "${ROOT}" \
    -mindepth 1 \
    -maxdepth 2 \
    -printf '%T@\t%TY-%Tm-%Td %TH:%TM:%TS\t%y\t%p\n' \
    2>/dev/null \
    | sort -nr \
    | head -n 100 \
    | cut -f2-

echo
echo
echo "============================================================"
echo "DONE"
echo "============================================================"
echo
echo "Inventory file:"
echo "${OUT}"
