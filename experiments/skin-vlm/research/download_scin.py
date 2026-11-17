"""Fetch a bounded SCIN review pool from its original, versioned GCS objects.

Condition labels select review candidates; they never become app appearance
labels automatically. No training-ready manifest is emitted by this command.
"""
from __future__ import annotations

import argparse
import ast
import base64
from collections import Counter, defaultdict, deque
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import time
import urllib.error
import urllib.parse
import urllib.request

BUCKET = "dx-scin-public-data"
SOURCE = "https://github.com/google-research-datasets/scin"
LICENSE_URL = "https://raw.githubusercontent.com/google-research-datasets/scin/main/LICENSE"


def get(url: str, limit: int = 24 * 1024 * 1024) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read(limit + 1)
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 3:
                raise
            time.sleep(.5 * 2 ** attempt)
    if len(data) > limit:
        raise ValueError("Object exceeds download limit: " + url)
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_object(name: str, output: Path) -> dict:
    if not name.startswith("dataset/") or ".." in PurePosixPath(name).parts:
        raise ValueError("Unexpected SCIN object path")
    encoded = urllib.parse.quote(name, safe="")
    api = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{encoded}"
    metadata = json.loads(get(api))
    generation = str(metadata["generation"])
    if int(metadata["size"]) > 24 * 1024 * 1024:
        raise ValueError("SCIN object exceeds size budget")
    # Pin the media request to the inspected generation; do not race a release.
    url = api + "?alt=media&generation=" + generation
    target = output / name
    data = target.read_bytes() if target.is_file() else get(url)
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    if len(data) != int(metadata["size"]) or md5 != metadata["md5Hash"]:
        raise ValueError("SCIN checksum/size mismatch: " + name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".partial")
        temporary.write_bytes(data)
        temporary.replace(target)
    return {"object": name, "generation": generation, "bytes": len(data),
            "md5_base64": md5, "sha256": sha256(data), "source_url": url}


def conditions(label: dict) -> dict:
    parsed = ast.literal_eval(label.get("weighted_skin_condition_label") or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("Invalid SCIN condition label")
    return {str(key): float(value) for key, value in parsed.items()}


def source_yes(value: str) -> bool:
    # The original CSV uses YES/empty, despite the schema describing booleans.
    return value.strip().lower() in {"yes", "true", "1"}


def review_bucket(case: dict, label: dict) -> str:
    names = " ".join(conditions(label)).lower()
    if "acne" in names:
        return "acne_like_bumps"
    if "hyperpigment" in names or "melasma" in names:
        return "dark_marks"
    if "rosacea" in names:
        return "redness"
    if source_yes(case.get("textures_rough_or_flaky", "")):
        return "flaking"
    return "other_or_uncertain"


def select_cases(cases: list[dict], labels: dict, limit: int) -> list[tuple]:
    # Round-robin by review need and supplied skin-tone strata, before images
    # or model predictions are inspected. Same case is selected only once.
    buckets = defaultdict(list)
    for case in cases:
        label = labels.get(case["case_id"])
        if not label or not source_yes(case.get("body_parts_head_or_neck", "")):
            continue
        if not any(case.get(f"image_{i}_path") for i in range(1, 4)):
            continue
        group = review_bucket(case, label)
        tone = label.get("monk_skin_tone_label_us") or label.get("monk_skin_tone_label_india") or "unknown"
        buckets[(group, tone)].append((case, label, group, tone))
    queues = {key: deque(sorted(rows, key=lambda row: sha256(row[0]["case_id"].encode())))
              for key, rows in buckets.items()}
    selected = []
    while len(selected) < limit and any(queues.values()):
        for key in sorted(queues):
            if queues[key] and len(selected) < limit:
                selected.append(queues[key].popleft())
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-images", type=int, default=0,
                        help="0 fetches metadata only; at most one image per selected case")
    parser.add_argument('--exclude-queue',type=Path,action='append',default=[])
    args = parser.parse_args()
    if not 0 <= args.max_images <= 512:
        raise ValueError("Image budget must be 0..512")
    if (args.output / "provenance.json").exists():
        raise FileExistsError("Completed snapshot exists; use a new versioned output")
    args.output.mkdir(parents=True, exist_ok=True)
    license_data = get(LICENSE_URL)
    (args.output / "SCIN-LICENSE.txt").write_bytes(license_data)
    objects = [fetch_object(f"dataset/{name}.csv", args.output)
               for name in ("scin_cases", "scin_labels")]
    def read_csv(name):
        return list(csv.DictReader(io.StringIO((args.output / f"dataset/{name}.csv").read_text())))
    cases = read_csv("scin_cases")
    labels = {row["case_id"]: row for row in read_csv("scin_labels")}
    excluded=set();exclusion_records=[]
    for path in args.exclude_queue:
        data=path.read_bytes();records=[json.loads(line) for line in data.decode().splitlines() if line.strip()]
        excluded.update(row['id'].removeprefix('scin-') for row in records)
        exclusion_records.append({'path':str(path.resolve()),'sha256':sha256(data),'rows':len(records)})
    cases=[case for case in cases if case['case_id'] not in excluded]
    rows, failures = [], []
    for case, label, bucket, tone in select_cases(cases, labels, args.max_images):
        # Prefer close-up, retaining the actual source shot type for review.
        slots = sorted(range(1, 4), key=lambda i: (case.get(f"image_{i}_shot_type") != "CLOSE_UP", i))
        index = next(i for i in slots if case.get(f"image_{i}_path"))
        name = case[f"image_{index}_path"]
        try:
            record = fetch_object(name, args.output)
        except Exception as exc:
            failures.append({"case_id": case["case_id"], "object": name, "error": str(exc)})
            continue
        objects.append(record)
        rows.append({"id": "scin-" + case["case_id"], "group_id": "scin-case-" + case["case_id"],
                     "group_unit": "source case, not a verified unique person",
                     "image": name, "sha256": record["sha256"], "source": SOURCE,
                     "license": "SCIN Data Use License", "license_record": "SCIN-LICENSE.txt",
                     "label_origin": "appearance_review_pending", "training_eligible": False,
                     "candidate_tag_for_review": bucket, "source_condition_hints": conditions(label),
                     "source_monk_skin_tone": tone, "shot_type": case.get(f"image_{index}_shot_type"),
                     "device_id": None, "captured_date": None,
                     "review": {"target": None, "reviewer": None, "reviewed_at": None}})
        print(json.dumps({"downloaded": len(rows), "budget": args.max_images}), flush=True)
    (args.output / "review_queue.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    report = {"source": SOURCE, "license_url": LICENSE_URL, "license_sha256": sha256(license_data),
              'excluded_cases':len(excluded),'exclusion_queues':exclusion_records,
              "source_cases": len(cases), "source_labelled_cases": len(labels), "downloaded_images": len(rows),
              "training_ready_images": 0, "review_buckets": dict(Counter(row["candidate_tag_for_review"] for row in rows)),
              "files": objects, "failures": failures,
              "limitations": ["Source diagnoses do not establish visible app appearance labels.",
                              "Head/neck metadata does not guarantee the selected image depicts a face.",
                              "Deduplicate exact/perceptual images across all splits before training.",
                              "Retain license and attribution; no re-identification."]}
    (args.output / "provenance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: report[key] for key in ("source_cases", "downloaded_images", "training_ready_images", "review_buckets")}, indent=2))


if __name__ == "__main__":
    main()
