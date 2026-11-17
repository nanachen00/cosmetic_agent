#!/usr/bin/env python3
"""Register already-restored model files without downloading or moving them."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    print(f"[HASH] {path}", flush=True)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--observed-revision", required=True)
    parser.add_argument(
        "--root", default=os.environ.get("GOODAGENTS_ROOT", "/mnt/goodagents")
    )
    args = parser.parse_args()

    runtime_dir = Path(__file__).resolve().parent
    manifest = json.loads((runtime_dir / "models.manifest.json").read_text())
    try:
        record = manifest["models"][args.model]
    except KeyError as exc:
        raise SystemExit(f"unknown model key: {args.model}") from exc

    expected_revision = record.get("revision")
    if not expected_revision:
        raise SystemExit(f"{args.model} has no official upstream revision")
    if args.observed_revision != expected_revision:
        raise SystemExit(
            "revision mismatch: "
            f"manifest={expected_revision} observed={args.observed_revision}"
        )

    root = Path(args.root)
    if root != Path("/mnt/goodagents"):
        raise SystemExit("artifact root must be /mnt/goodagents")
    target = root / record["path"]
    if not target.is_dir():
        raise SystemExit(f"missing model directory: {target}")
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SystemExit(f"model resolves outside persistent mount: {target}") from exc
    if not (target / "config.json").is_file():
        raise SystemExit(f"missing config.json: {target}")

    metadata_path = target / ".blink-artifact.json"
    if metadata_path.exists():
        raise SystemExit(
            f"metadata already exists: {metadata_path}; verify it instead of replacing it"
        )

    weights = sorted(target.glob("*.safetensors")) + sorted(
        target.glob("pytorch_model*.bin")
    )
    if not weights:
        raise SystemExit(f"no recognized model weights: {target}")

    files = {path.name: sha256(path) for path in weights}
    official = record.get("weight_sha256") or {}
    failures = [
        name
        for name, expected in official.items()
        if files.get(name) != expected
    ]
    if failures:
        raise SystemExit(
            "official checksum mismatch: " + ", ".join(sorted(failures))
        )

    metadata = {
        "schema_version": 1,
        "repo_id": record["repo_id"],
        "revision": expected_revision,
        "files": files,
        "registered_from_existing": True,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = metadata_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, metadata_path)
    print(f"[OK] registered {args.model}: {metadata_path}")
    for name, checksum in sorted(files.items()):
        print(f"  {checksum}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
