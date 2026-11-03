#!/usr/bin/env python3
"""Inventory a project without reading file contents or following directory links.

Python 3.8+, standard library only. The current output directory is excluded.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import stat
import sys
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Project directory on the VM")
    parser.add_argument("output", help="New output directory; may be inside project/ops")
    args = parser.parse_args()
    root = Path(args.project).expanduser().resolve(strict=True)
    if not root.is_dir():
        parser.error("project must be a directory")
    output = Path(args.output).expanduser().resolve()
    if output == root:
        parser.error("output must be a separate directory, not the project root")
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite a prior inventory.
    counts = {"file": 0, "directory": 0, "symlink": 0, "reparse_point": 0, "other": 0}
    errors = 0
    started = datetime.now(timezone.utc).isoformat()

    with (output / "inventory.jsonl").open("x", encoding="utf-8") as inventory, \
            (output / "errors.jsonl").open("x", encoding="utf-8") as error_log:
        def record_error(path, exc):
            nonlocal errors
            errors += 1
            error_log.write(json.dumps({"relative_path": path.relative_to(root).as_posix(),
                                       "error": type(exc).__name__, "errno": exc.errno,
                                       "winerror": getattr(exc, "winerror", None)},
                                      ensure_ascii=True) + "\n")

        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if path == output:
                            continue  # Exclude only this run's output, not all of ops.
                        try:
                            info = entry.stat(follow_symlinks=False)
                            is_reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
                            if stat.S_ISLNK(info.st_mode):
                                kind = "symlink"
                            elif is_reparse:
                                kind = "reparse_point"  # Includes Windows junctions.
                            elif stat.S_ISDIR(info.st_mode):
                                kind = "directory"
                            elif stat.S_ISREG(info.st_mode):
                                kind = "file"
                            else:
                                kind = "other"
                            row = {"relative_path": path.relative_to(root).as_posix(),
                                   "type": kind,
                                   "size_bytes": info.st_size if kind == "file" else None,
                                   "modified_time_ns": info.st_mtime_ns}
                            inventory.write(json.dumps(row, ensure_ascii=True) + "\n")
                            counts[kind] += 1
                            if kind == "directory":
                                pending.append(path)
                        except OSError as exc:
                            record_error(path, exc)
            except OSError as exc:
                record_error(directory, exc)

    summary = {"schema_version": 2, "project_root": str(root),
               "output_directory": str(output), "excluded_paths": [str(output)],
               "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
               "os": platform.system(), "os_release": platform.release(),
               "python": platform.python_version(), "counts": counts, "error_count": errors,
               "status": "COMPLETED_WITH_ERRORS" if errors else "COMPLETED",
               "file_contents_read": False, "hashes_computed": False,
               "limitations": ["The current output directory is intentionally excluded.",
                               "Directory links/reparse points are listed but not traversed.",
                               "This is a live listing, not an atomic filesystem snapshot.",
                               "Inaccessible or disappearing entries are recorded in errors.jsonl.",
                               "Entry sizes are logical sizes, not physical disk allocation."]}
    with (output / "summary.json").open("x", encoding="utf-8") as summary_file:
        json.dump(summary, summary_file, ensure_ascii=True, indent=2)
        summary_file.write("\n")
    print(json.dumps({"output": str(output), "status": summary["status"],
                      "counts": counts, "error_count": errors}, ensure_ascii=True, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
