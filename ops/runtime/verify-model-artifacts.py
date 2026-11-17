#!/usr/bin/env python3
"""Verify selected model artifacts without downloading or repairing anything."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="taide-prod,embedding")
    parser.add_argument(
        "--root", default=os.environ.get("GOODAGENTS_ROOT", "/mnt/goodagents")
    )
    args = parser.parse_args()

    runtime_dir = Path(__file__).resolve().parent
    manifest = json.loads((runtime_dir / "models.manifest.json").read_text())
    selected = [item.strip() for item in args.models.split(",") if item.strip()]
    unknown = sorted(set(selected) - set(manifest["models"]))
    if unknown:
        raise SystemExit(f"unknown model keys: {', '.join(unknown)}")

    root = Path(args.root)
    failures: list[str] = []
    for key in selected:
        record = manifest["models"][key]
        target = root / record["path"]
        if not target.is_dir():
            failures.append(f"{key}: missing directory {target}")
            continue
        if record.get("repo_id") and not (target / "config.json").is_file():
            failures.append(f"{key}: missing config.json")
            continue

        metadata_path = target / ".blink-artifact.json"
        if not metadata_path.is_file():
            failures.append(f"{key}: missing download-time .blink-artifact.json")
            continue
        try:
            local_metadata = json.loads(metadata_path.read_text())
        except (OSError, ValueError) as exc:
            failures.append(f"{key}: invalid artifact metadata ({type(exc).__name__})")
            continue
        if local_metadata.get("repo_id") != record.get("repo_id"):
            failures.append(f"{key}: repo_id differs from manifest")
        if local_metadata.get("revision") != record.get("revision"):
            failures.append(f"{key}: revision differs from manifest")

        expected = dict(local_metadata.get("files") or {})
        expected.update(record.get("weight_sha256") or {})
        if not expected:
            failures.append(f"{key}: no recorded file checksums")
            continue
        for relative, wanted in sorted(expected.items()):
            artifact = target / relative
            if not artifact.is_file():
                failures.append(f"{key}: missing {relative}")
                continue
            actual = sha256(artifact)
            if actual != wanted:
                failures.append(f"{key}: checksum mismatch {relative}")
        if not any(target.glob("*.safetensors")) and not any(
            target.glob("pytorch_model*.bin")
        ):
            failures.append(f"{key}: no model weight file")
        failed = any(item.startswith(key + ":") for item in failures)
        print(f"[{'FAIL' if failed else 'OK'}] {key} {target}")

    if failures:
        print("Artifact verification failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
