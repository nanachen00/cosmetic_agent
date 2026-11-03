cat > /var/lib/jenkins/tomod/ops/results/collect-blink-intent-workspace-tree.sh <<'BASH'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark"
RESULTS_ROOT="/var/lib/jenkins/tomod/ops/results"

if [[ ! -d "$ROOT" ]]; then
    echo "ERROR: workspace not found: $ROOT" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || {
    echo "ERROR: python3 is required" >&2
    exit 1
}

STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT_DIR="${RESULTS_ROOT}/blink-root-intent-inventory-${STAMP}"

TREE_TXT="${OUT_DIR}/workspace-tree.txt"
MANIFEST_JSON="${OUT_DIR}/workspace-manifest.json"
CONTEXT_TXT="${OUT_DIR}/workspace-context.txt"

ARCHIVE="${RESULTS_ROOT}/blink-root-intent-inventory-${STAMP}.tar.gz"

mkdir -p "$OUT_DIR"

export ROOT OUT_DIR TREE_TXT MANIFEST_JSON CONTEXT_TXT

python3 <<'PY'
import json
import os
import stat
import socket
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ["ROOT"])
tree_txt = Path(os.environ["TREE_TXT"])
manifest_json = Path(os.environ["MANIFEST_JSON"])

# 只略過與 Gate 原始碼/證據判讀無關、而且可能非常龐大的開發快取。
# .models 不略過，因為目前 Kev / model pointer 資訊可能與 Gate 測試有關。
PRUNE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
}

records = []
summary = {
    "directories": 0,
    "files": 0,
    "symlinks": 0,
    "file_bytes": 0,
    "pruned_directories": 0,
    "errors": 0,
}

def iso_mtime(st):
    return datetime.fromtimestamp(
        st.st_mtime, tz=timezone.utc
    ).isoformat()

def mode_string(st_mode):
    return stat.filemode(st_mode)

def relative(path: Path):
    if path == root:
        return "."
    return str(path.relative_to(root))

def add_record(path: Path, kind: str, pruned=False, error=None):
    try:
        st = path.lstat()

        item = {
            "path": relative(path),
            "type": kind,
            "mode": mode_string(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mtime_utc": iso_mtime(st),
            "pruned": pruned,
        }

        if kind == "file":
            item["size_bytes"] = st.st_size
            summary["files"] += 1
            summary["file_bytes"] += st.st_size

        elif kind == "directory":
            summary["directories"] += 1
            if pruned:
                summary["pruned_directories"] += 1

        elif kind == "symlink":
            summary["symlinks"] += 1
            try:
                item["target"] = os.readlink(path)
            except OSError as exc:
                item["target_error"] = str(exc)

        if error is not None:
            item["error"] = str(error)

        records.append(item)

    except OSError as exc:
        summary["errors"] += 1
        records.append({
            "path": relative(path),
            "type": kind,
            "error": str(exc),
        })

def human_size(n):
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(n)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024

def sorted_entries(path: Path):
    try:
        items = list(path.iterdir())
    except (PermissionError, OSError) as exc:
        return None, exc

    def key(p):
        try:
            is_dir = p.is_dir() and not p.is_symlink()
        except OSError:
            is_dir = False
        return (not is_dir, p.name.lower())

    return sorted(items, key=key), None

lines = []
lines.append(str(root))
add_record(root, "directory")

def walk(path: Path, prefix=""):
    entries, error = sorted_entries(path)

    if error:
        summary["errors"] += 1
        lines.append(prefix + f"[ERROR reading directory: {error}]")
        return

    for index, entry in enumerate(entries):
        last = index == len(entries) - 1
        branch = "└── " if last else "├── "
        child_prefix = prefix + ("    " if last else "│   ")

        try:
            if entry.is_symlink():
                try:
                    target = os.readlink(entry)
                except OSError:
                    target = "<unreadable>"

                lines.append(
                    prefix + branch + entry.name + " -> " + target
                )
                add_record(entry, "symlink")
                continue

            if entry.is_dir():
                if entry.name in PRUNE_DIRS:
                    lines.append(
                        prefix + branch + entry.name + "/ [PRUNED]"
                    )
                    add_record(entry, "directory", pruned=True)
                    continue

                lines.append(prefix + branch + entry.name + "/")
                add_record(entry, "directory")
                walk(entry, child_prefix)
                continue

            try:
                size = entry.stat().st_size
                size_text = human_size(size)
            except OSError:
                size_text = "unknown size"

            lines.append(
                prefix + branch + f"{entry.name} [{size_text}]"
            )
            add_record(entry, "file")

        except (PermissionError, OSError) as exc:
            summary["errors"] += 1
            lines.append(
                prefix + branch + f"{entry.name} [ERROR: {exc}]"
            )
            add_record(entry, "unknown", error=exc)

walk(root)

header = [
    "# BLINK Intent Parse Router workspace inventory",
    "",
    f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
    f"Host: {socket.gethostname()}",
    f"Root: {root}",
    "",
    "# Notes",
    "",
    "- File contents were NOT read.",
    "- No workspace files were modified.",
    "- No SHA-256 hashing was performed.",
    "- Generic dependency/cache directories may be marked [PRUNED].",
    "- .models is intentionally included in the structural inventory.",
    "",
    "# Tree",
    "",
]

tree_txt.write_text(
    "\n".join(header + lines) + "\n",
    encoding="utf-8",
)

manifest = {
    "schema": "blink-root-intent-workspace-inventory-v1",
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "root": str(root),
    "pruned_directory_names": sorted(PRUNE_DIRS),
    "summary": summary,
    "entries": records,
}

manifest_json.write_text(
    json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)

print("Inventory summary:")
print(json.dumps(summary, indent=2))
PY

{
    echo "# Workspace context"
    echo
    echo "Generated: $(date --iso-8601=seconds 2>/dev/null || date)"
    echo "Root: $ROOT"
    echo
    echo "## Host"
    uname -a || true
    echo
    echo "## Disk usage"
    du -sh "$ROOT" 2>/dev/null || true
    echo
    echo "## Git"

    if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "repo=true"
        echo "branch=$(git -C "$ROOT" branch --show-current 2>/dev/null || true)"
        echo "commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
        echo
        echo "### git status --short"
        git -C "$ROOT" status --short 2>/dev/null || true
    else
        echo "repo=false"
    fi
} > "$CONTEXT_TXT"

tar -czf "$ARCHIVE" \
    -C "$OUT_DIR" \
    workspace-tree.txt \
    workspace-manifest.json \
    workspace-context.txt

echo
echo "============================================================"
echo "INVENTORY COMPLETE"
echo "============================================================"
echo "Tree:"
echo "  $TREE_TXT"
echo
echo "Manifest:"
echo "  $MANIFEST_JSON"
echo
echo "Context:"
echo "  $CONTEXT_TXT"
echo
echo "UPLOAD THIS FILE:"
echo "  $ARCHIVE"
echo
echo "No files under the benchmark workspace were modified."
BASH

chmod +x /var/lib/jenkins/tomod/ops/results/collect-blink-intent-workspace-tree.sh

/var/lib/jenkins/tomod/ops/results/collect-blink-intent-workspace-tree.sh