"""Create a read-only AI Stack inventory without reading secret file contents."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path("/var/lib/jenkins/tomod")
OUTPUT = Path("/tmp/blinkai_ai_stack_inventory.json")
RUNTIME_NAMES = {
    "cosmetic-agent",
    "cosmetic-agent-stable",
    "frontend",
}


def category(path: Path) -> str:
    name = path.name.lower()
    if path.name in RUNTIME_NAMES:
        return "runtime-protected"
    if name.endswith((".bak", ".old", ".orig", ".zip", ".tar", ".tar.gz", ".tgz")):
        return "archive-candidate"
    if name.endswith((".log", ".out")):
        return "log-candidate"
    if name.startswith(("restart", "deploy", "start", "health")):
        return "ops-script-review"
    return "unclassified"


def main() -> None:
    entries = []
    for path in sorted(ROOT.iterdir(), key=lambda item: item.name.lower()):
        stat = path.stat()
        entries.append({
            "name": path.name,
            "kind": "directory" if path.is_dir() else "file",
            "category": category(path),
            "size_bytes": stat.st_size if path.is_file() else None,
        })
    report = {
        "root": str(ROOT),
        "destructive_changes": False,
        "entries": entries,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    counts = {}
    for entry in entries:
        counts[entry["category"]] = counts.get(entry["category"], 0) + 1
    print("AI_STACK_INVENTORY_OK")
    print(f"report={OUTPUT}")
    for name, count in sorted(counts.items()):
        print(f"{name}={count}")


if __name__ == "__main__":
    main()

