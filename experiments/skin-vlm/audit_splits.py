"""Audit real, permitted, independently labelled photo splits before training."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from core import TAGS, load_rows
from prepare_data import assert_no_leakage


def main():
    parser = argparse.ArgumentParser()
    for split in ("train", "val", "test", "external"):
        parser.add_argument("--" + split, type=Path, required=True)
    args = parser.parse_args()
    groups = {name: load_rows(getattr(args, name)) for name in ("train", "val", "test", "external")}
    assert_no_leakage(*groups.values())
    report = {"scope": "Data integrity and coverage; no model accuracy claim", "splits": {}, "gaps": []}
    for name, rows in groups.items():
        if any(row["label_origin"] != "human_verified" for row in rows):
            raise ValueError(f"{name}: independently verified labels required")
        positives = Counter(tag for row in rows for tag in {o["tag"] for o in row["target"]["observations"]})
        coverage = {tag: {"positive": positives[tag], "negative": len(rows) - positives[tag]} for tag in sorted(TAGS)}
        for tag, counts in coverage.items():
            if not counts["positive"] or not counts["negative"]:
                report["gaps"].append(f"{name}/{tag}: missing positive or negative examples")
        missing_strata = [field for field in ("device_id", "skin_tone", "capture_profile") if any(not row.get(field) for row in rows)]
        report["splits"][name] = {"images": len(rows), "declared_subjects": len({row["subject_id"] for row in rows}),
            "coverage": coverage, "missing_strata": missing_strata,
            "manifest_sha256": hashlib.sha256(getattr(args, name).read_bytes()).hexdigest()}
        report["gaps"].extend(f"{name}: {field} missing" for field in missing_strata)
    report["verdict"] = "PASS" if not report["gaps"] else "INCOMPLETE"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["gaps"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
