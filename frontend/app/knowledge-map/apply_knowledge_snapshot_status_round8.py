from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_knowledge_snapshot_status_round8.py SERVICE")
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    old = '''            snapshot_status = snapshot.status()
            if not snapshot_status["available"] or not snapshot_status["node_count"]:
                raise RuntimeError("Knowledge map JSON is missing or empty")
'''
    new = '''            snapshot_status = snapshot.status()
            snapshot_available = bool(
                snapshot_status.get("available", snapshot_status.get("ok", False))
            )
            snapshot_node_count = snapshot_status.get("node_count")
            if snapshot_node_count is None:
                # Compatibility for read-only repository adapters that expose
                # only an ``ok`` health flag. Actual JSON snapshots still
                # report and validate their concrete node count.
                snapshot_node_count = 1 if snapshot_available else 0
            if not snapshot_available or not snapshot_node_count:
                raise RuntimeError("Knowledge map JSON is missing or empty")
'''
    count = source.count(old)
    if count == 0 and "snapshot_available = bool(" in source:
        print("knowledge snapshot status round 8 already applied")
        return
    if count != 1:
        raise RuntimeError(f"snapshot status check: expected one match, found {count}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("knowledge snapshot status round 8 applied")


if __name__ == "__main__":
    main()
