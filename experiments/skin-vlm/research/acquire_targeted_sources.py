#!/usr/bin/env python3
"""Small, version-pinned MEMI-DS intake. Acquired files are never training gold."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
import uuid

METADATA_URL = "https://api.figshare.com/v2/articles/29209229/versions/1"
SOURCE_PAGE = "https://figshare.com/articles/figure/MEMI-DS_A_Benchmark_Melasma_Image_Dataset_for_Image_Segmentation/29209229"
PARTIAL_EVIDENCE = "https://link.springer.com/chapter/10.1007/978-981-95-3456-2_29"
MOUNT = Path("/mnt/goodagents")
NFS_SOURCE = "192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11"
INTAKE_ROOT = MOUNT / "blink-runtime/data/training-intake"
DEFAULT_OUTPUT = INTAKE_ROOT / "memi-ds-v1-20261116"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SELECTION_BYTES = 50 * 1024 * 1024


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_entry(item):
    name = item.get("name", "")
    if not re.fullmatch(r"[0-9]+_[FLR]\.(?:jpg|png)", name):
        raise ValueError("Unexpected source filename")
    md5 = item.get("computed_md5", "")
    if not re.fullmatch(r"[0-9a-f]{32}", md5) or item.get("supplied_md5") != md5:
        raise ValueError("Missing or inconsistent publisher MD5")
    size = item.get("size")
    if type(size) is not int or not 0 < size <= MAX_FILE_BYTES:
        raise ValueError("Source file exceeds the small-file budget")
    file_id = item.get("id")
    url = f"https://ndownloader.figshare.com/files/{file_id}"
    if type(file_id) is not int or item.get("download_url") != url or item.get("is_link_only"):
        raise ValueError("Unexpected source download URL")
    return {"id": file_id, "name": name, "bytes": size, "publisher_md5": md5, "download_url": url}


def select_files(document, count=8):
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("Select between one and eight frontal images")
    if document.get("id") != 29209229 or document.get("version") != 1:
        raise ValueError("Wrong source release")
    if document.get("license", {}).get("url") != "https://creativecommons.org/licenses/by/4.0/":
        raise ValueError("Source license differs from the verified CC BY 4.0 release")
    catalog = [file_entry(item) for item in document.get("files", [])]
    if sum(item["bytes"] for item in catalog) > 300 * 1024 * 1024:
        raise ValueError("Published release exceeds 300 MiB intake policy")
    by_name = {item["name"]: item for item in catalog}
    if len(by_name) != len(catalog):
        raise ValueError("Duplicate source filenames")
    frontal = sorted((item for item in catalog if item["name"].endswith("_F.jpg")), key=lambda item: int(item["name"].split("_")[0]))[:count]
    if len(frontal) != count:
        raise ValueError("Insufficient frontal source candidates")
    selected = []
    for photo in frontal:
        annotation = by_name.get(photo["name"].replace(".jpg", ".png"))
        if annotation is None:
            raise ValueError("Missing paired segmentation PNG")
        group = "memi-ds-v1-source-prefix-" + photo["name"].split("_")[0]
        selected.extend([{**photo, "role": "photo_candidate", "leakage_group": group}, {**annotation, "role": "paired_segmentation_png_candidate", "leakage_group": group}])
    if sum(item["bytes"] for item in selected) > MAX_SELECTION_BYTES:
        raise ValueError("Selected files exceed 50 MiB budget")
    return catalog, selected


def validate_mount(record):
    if (record.get("target") != str(MOUNT) or record.get("source") != NFS_SOURCE
            or record.get("fstype") not in {"nfs", "nfs4"}
            or "rw" not in record.get("options", "").split(",")):
        raise ValueError("Required persistent NFS source is not mounted read-write")


def verify_target(target):
    if not target.is_absolute() or ".." in target.parts or not target.is_relative_to(INTAKE_ROOT) or target == INTAKE_ROOT:
        raise ValueError("Target must be inside the isolated persistent intake root")
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError("Symlink intake target is forbidden")


def hashes(path):
    md5, sha256 = hashlib.md5(), hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            md5.update(chunk)
            sha256.update(chunk)
    return {"bytes": size, "md5": md5.hexdigest(), "sha256": sha256.hexdigest()}


def download_one(item, directory, opener=urllib.request.urlopen):
    # Selection already checked source names/URLs; recheck before a path is joined.
    if not re.fullmatch(r"[0-9]+_F\.(?:jpg|png)", item["name"]):
        raise ValueError("Unsafe selected filename")
    target = directory / item["name"]
    if target.is_symlink():
        raise ValueError("Symlink source file is forbidden")
    expected = (item["bytes"], item["publisher_md5"])
    if target.exists():
        actual = hashes(target)
        if (actual["bytes"], actual["md5"]) != expected:
            raise ValueError("Existing source file failed publisher checksum; retained untouched")
        return {"name": item["name"], **actual, "reused_verified_file": True}
    partial = directory / (item["name"] + ".partial")
    if partial.is_symlink():
        raise ValueError("Symlink partial is forbidden")
    if partial.exists():
        actual = hashes(partial)
        if (actual["bytes"], actual["md5"]) == expected:
            os.replace(partial, target)
            return {"name": item["name"], **actual, "reused_verified_file": True}
        partial.rename(directory / (partial.name + ".interrupted-" + uuid.uuid4().hex))
    request = urllib.request.Request(item["download_url"], headers={"User-Agent": "BLINK-targeted-source-intake/1.0"})
    with opener(request, timeout=45) as response, partial.open("xb") as sink:
        size = 0
        while chunk := response.read(1024 * 256):
            size += len(chunk)
            if size > item["bytes"]:
                raise ValueError("Download exceeded publisher size")
            sink.write(chunk)
        sink.flush()
        os.fsync(sink.fileno())
    actual = hashes(partial)
    if (actual["bytes"], actual["md5"]) != expected:
        raise ValueError("Downloaded file failed publisher checksum; partial retained")
    os.replace(partial, target)
    return {"name": item["name"], **actual, "reused_verified_file": False}


def write_report(path, report):
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Symlink report parent is forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if path.is_symlink() or partial.is_symlink():
        raise ValueError("Symlink report path is forbidden")
    partial.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    os.replace(partial, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--blocked-reason", default="")
    args = parser.parse_args()
    with urllib.request.urlopen(urllib.request.Request(METADATA_URL, headers={"Accept": "application/json"}), timeout=30) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Source metadata exceeds budget")
    document = json.loads(raw)
    catalog, selected = select_files(document, args.count)
    report = {
        "status": "metadata_ready_download_not_started", "updated_at": timestamp(),
        "source": {"title": document["title"], "metadata_url": METADATA_URL, "metadata_sha256": hashlib.sha256(raw).hexdigest(), "version": 1,
                   "doi": document["doi"], "source_page": SOURCE_PAGE, "license": document["license"], "published_date": document.get("published_date"),
                   "partial_release": True, "partial_release_evidence": PARTIAL_EVIDENCE},
        "selection_rule": "First up to eight numeric source prefixes with frontal _F.jpg and paired _F.png; no diagnostic or model prediction used to select; all available views of a prefix must stay in one split.",
        "catalog_file_count": len(catalog), "catalog_bytes": sum(item["bytes"] for item in catalog), "published_file_manifest": catalog,
        "selected_photo_count": args.count, "selected_file_count": len(selected), "selected_bytes": sum(item["bytes"] for item in selected), "selected_files": selected,
        "destination": str(args.output), "blocking_condition": args.blocked_reason or None, "verified_downloads": [], "download_complete": False,
        "limitations": ["Isolated public-source candidates only; not added to any train/validation/test set.", "Melasma diagnosis and segmentation PNGs are not our dark_marks/flaking appearance gold.", "No per-image phone/device capture proof or healthy-face/flaking gold verified.", "Source-prefix grouping is conservative; actual patient identifiers and annotation methodology remain to be verified."],
    }
    if args.download:
        verify_target(args.output)
        if not args.report.is_absolute() or ".." in args.report.parts or not args.report.is_relative_to(args.output):
            raise ValueError("Download report must stay inside the verified NFS intake target")
        mount = json.loads(subprocess.check_output(["findmnt", "-J", "--mountpoint", str(MOUNT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], text=True))["filesystems"][0]
        validate_mount(mount)
        args.output.mkdir(parents=True, exist_ok=True)
        report["mount_verified"] = mount
    write_report(args.report, report)
    if args.download:
        report["status"] = "partial_download_in_progress"
        try:
            for item in selected:
                report["verified_downloads"].append(download_one(item, args.output))
                report["updated_at"] = timestamp()
                write_report(args.report, report)
            report["download_complete"] = True
            report["blocking_condition"] = None
            report["status"] = "isolated_candidates_downloaded_not_gold"
        except Exception as exc:
            report["status"] = "partial_download_blocked"
            report["blocking_condition"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            report["updated_at"] = timestamp()
            write_report(args.report, report)
    print(json.dumps({key: report[key] for key in ["status", "selected_photo_count", "selected_file_count", "selected_bytes", "download_complete", "blocking_condition"]}))


if __name__ == "__main__":
    main()
