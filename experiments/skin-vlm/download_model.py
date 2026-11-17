"""Download a pinned official Qwen snapshot and verify source LFS SHA-256s."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
REPO = "Qwen/Qwen3-VL-4B-Instruct"
REVISION = "ebb281ec70b05090aa6165b016eac8ec08e71b17"
WEIGHTS = {
    "model-00001-of-00002.safetensors": "30a01a0556622645a3cce87b655bbbbbc1f170c196099f1b666c93202c3339a9",
    "model-00002-of-00002.safetensors": "046296a2a387efb43b0c997d5833c789604d168834f6e0d3064bf7bb13d002a6",
}


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "model")
    parser.add_argument("--persistent-root", type=Path)
    args = parser.parse_args()
    if args.persistent_root:
        root = args.persistent_root.resolve()
        mount = json.loads(subprocess.check_output(["findmnt", "-J", "-T", str(root)], text=True))["filesystems"][0]
        if root != Path("/mnt/goodagents") or mount["target"] != str(root) or mount["fstype"] not in {"nfs", "nfs4"}:
            raise RuntimeError("Expected /mnt/goodagents NFS mount; refusing container-layer download")
        if not args.out.resolve().is_relative_to(root):
            raise ValueError("Model output must remain on the persistent mount")
    from huggingface_hub import HfApi, snapshot_download
    info = HfApi().model_info(REPO, revision=REVISION, files_metadata=True)
    if info.sha != REVISION:
        raise RuntimeError("Official model revision mismatch")
    official = {entry.rfilename: entry.lfs.sha256 for entry in info.siblings if entry.lfs and entry.rfilename in WEIGHTS}
    if official != WEIGHTS:
        raise RuntimeError("Official weight checksums differ from the recorded model manifest")
    print("DOWNLOADING", REPO, REVISION, flush=True)
    snapshot_download(REPO, revision=REVISION, local_dir=args.out,
                      allow_patterns=["*.json", "*.safetensors", "*.txt", "*.jinja", "README.md", "LICENSE*"], max_workers=2)
    for name, expected in WEIGHTS.items():
        if file_hash(args.out / name) != expected:
            raise RuntimeError("Weight checksum mismatch: " + name)
        print("VERIFIED", name, expected, flush=True)
    index = json.loads((args.out / "model.safetensors.index.json").read_text())
    if set(index["weight_map"].values()) != set(WEIGHTS):
        raise RuntimeError("Model index references unexpected/missing shards")
    files = {path.name: {"sha256": file_hash(path), "bytes": path.stat().st_size}
             for path in args.out.iterdir() if path.is_file() and path.name != "artifact-metadata.json"}
    (args.out / "artifact-metadata.json").write_text(json.dumps({"repo_id": REPO, "revision": REVISION,
        "source_url": f"https://huggingface.co/{REPO}/tree/{REVISION}", "official_weight_sha256": WEIGHTS,
        "files": files, "verified": True}, indent=2))
    print("DOWNLOAD_VERIFIED", str(args.out), flush=True)


if __name__ == "__main__":
    main()
