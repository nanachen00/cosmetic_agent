"""Clean AI-only candidates without opening protected images or changing gold.

No target prose, expert status, training admission or data splits are generated.
Protected fingerprints come from an existing audit, never from sealed images.
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research.clean_expansion import image_features, duplicate_clusters, apply_manual_groups, resolve_image

TAGS = frozenset(("acne_like_bumps", "redness", "dark_marks", "flaking"))
SOURCE = "https://github.com/google-research-datasets/scin"
FINGERPRINTS = ("normalized_rgb_sha256", "dhash64", "phash63", "mirrored_dhash64", "mirrored_phash63")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def load_sources(paths):
    sources, inputs = {}, []
    for path in paths:
        path = Path(path).resolve()
        provenance_path = path.parent / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        if provenance.get("source") != SOURCE or not valid_sha(provenance.get("license_sha256")):
            raise ValueError("Invalid SCIN source provenance")
        objects = {item["object"]: item for item in provenance.get("files", [])}
        inputs.extend((path, provenance_path))
        for row in rows(path):
            license_path = (path.parent / row.get("license_record", "")).resolve()
            image = (path.parent / row["image"]).resolve()
            if (row.get("source") != SOURCE or row.get("license") != "SCIN Data Use License"
                    or not license_path.is_relative_to(path.parent) or not license_path.is_file()
                    or digest(license_path) != provenance["license_sha256"]
                    or not image.is_relative_to(path.parent) or not valid_sha(row.get("sha256"))
                    or not isinstance(row.get("group_id"), str) or not row["group_id"]):
                raise ValueError("Invalid source license, image or group provenance")
            obj = objects.get(row["image"], {})
            if obj.get("sha256") != row["sha256"] or not str(obj.get("generation", "")).isdigit():
                raise ValueError("Versioned original object checksum missing")
            key = row["sha256"], row["group_id"]
            if key in sources:
                raise ValueError("Duplicate source identity")
            sources[key] = dict(row, resolved_image=str(image), license_sha256=provenance["license_sha256"],
                                object_generation=str(obj["generation"]), source_manifest=str(path))
            inputs.append(license_path)
    return sources, inputs


def load_reviews(paths):
    reviews = {}
    for path in paths:
        review_sha = digest(path)
        for row in rows(path):
            key = review_sha, row.get("image_sha256"), row.get("source_group")
            if key in reviews:
                raise ValueError("Duplicate AI evidence identity")
            reviews[key] = row
    return reviews


def protection_records(path):
    result = []
    for row in rows(path):
        if not (row.get("role") == "protected" or
                (row.get("role") in {"existing", "new"} and row.get("status") == "accepted")):
            continue
        if (not valid_sha(row.get("sha256")) or not valid_sha(row.get("normalized_rgb_sha256"))
                or any(type(row.get(key)) is not int or row[key] < 0 for key in FINGERPRINTS[1:])):
            raise ValueError("Protected fingerprint registry incomplete")
        # Deliberately discard paths and targets from the protected audit.
        result.append({key: row[key] for key in ("id", "sha256", *FINGERPRINTS,
                                               "declared_group", "manual_review_groups") if key in row})
        result[-1]["role"] = "protected"
    if not result:
        raise ValueError("A nonempty gold and prior-holdout registry is required")
    return result


def clean(queue, source_queues, ai_reviews, ocr_files, protection_audit, output,
          path_maps=(), manual_groups=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a new versioned candidate directory")
    sources, source_inputs = load_sources(source_queues)
    evidence = load_reviews(ai_reviews)
    protected = protection_records(protection_audit)
    input_paths = [Path(queue), Path(protection_audit), *map(Path, ai_reviews), *map(Path, ocr_files), *source_inputs]
    if manual_groups is not None:
        input_paths.append(Path(manual_groups))
    before = {str(path.resolve()): digest(path) for path in input_paths}
    ocr = {}
    for path in ocr_files:
        for row in rows(path):
            key = row.get("sha256")
            if not valid_sha(key):
                raise ValueError("OCR record must bind to image SHA256")
            if key in ocr and any(ocr[key].get(field) != row.get(field)
                                  for field in ("complete", "policy_version", "condition_token_hits")):
                raise ValueError("Conflicting OCR record")
            ocr[key] = row
    audit, records, seen = [], [], set()
    maps = [(Path(a).resolve(), Path(b).resolve()) for a, b in path_maps]
    for row in rows(queue):
        sha, group = row.get("image_sha256"), row.get("source_group")
        source = sources.get((sha, group))
        identity = sha, group
        if identity in seen:
            raise ValueError("Duplicate intake identity")
        seen.add(identity)
        entry = dict(id=source["id"] if source else "unbound-intake-" + str(len(audit)),
                     sha256=sha, declared_group=group, role="review", reasons=[], input=row)
        reasons = entry["reasons"]
        if source is None:
            reasons.append("source_identity_missing")
        if row.get("label_origin") != "ai_visual_review" or row.get("human_expert_verified") is not False or row.get("training_eligible") is not False:
            reasons.append("ai_provenance_or_admission_invalid")
        labels = row.get("candidate_labels")
        if not isinstance(labels, dict) or set(labels) != TAGS or any(type(v) is not bool for v in labels.values()):
            reasons.append("appearance_labels_missing_or_unknown")
        if row.get("existing_silver_candidate") is not True:
            reasons.append("not_existing_silver_candidate")
        review = evidence.get((row.get("source_review_sha256"), sha, group))
        if (review is None or review.get("label_origin") != "ai_visual_review"
                or review.get("human_expert_verified") is not False or review.get("training_eligible") is not False
                or review.get("tag_decisions", review.get("candidate_labels")) != labels
                or review.get("image_scope") != row.get("image_scope")
                or review.get("silver_candidate", review.get("existing_silver_candidate")) is not True):
            reasons.append("ai_evidence_unverified_or_not_silver")
        flags = row.get("quality_flags")
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            reasons.append("quality_review_missing")
            flags = []
        if review is not None:
            flags = list(set(flags) | set(review.get("quality_flags", [])))
        scope = row.get("image_scope")
        if scope not in {"face", "skin_closeup"}:
            reasons.append("scope_not_supported")
        if scope == "face" and "other_body_region" in flags:
            reasons.append("face_scope_conflicts_with_body_region")
        reasons.extend("quality:" + flag for flag in flags if flag != "other_body_region")
        check = ocr.get(sha)
        if not check or check.get("complete") is not True or check.get("policy_version") != 2 or not isinstance(check.get("condition_token_hits"), list):
            reasons.append("OCR_missing_or_failed")
        elif check["condition_token_hits"]:
            reasons.append("condition_text_label_leakage")
        if source is not None:
            entry["source"] = source
            try:
                if "image_path_on_gpu" in row:
                    declared_image = resolve_image(row["image_path_on_gpu"], Path(queue), maps)
                else:
                    declared_image = resolve_image(row["image_path_from_source_queue"], Path(source["source_manifest"]), maps)
                if declared_image != Path(source["resolved_image"]):
                    raise ValueError("intake_image_path_mismatch")
                features = image_features(declared_image, sha)
                entry.update(features)
                reasons.extend(features["quality_flags"])
                records.append(entry)
            except (OSError, ValueError, KeyError) as error:
                reasons.append(str(error) if isinstance(error, ValueError) else "image_missing_or_invalid")
        audit.append(entry)
    # Attach every checksum-bound manual member, including ineligible rows.
    if manual_groups is not None:
        apply_manual_groups([*protected, *audit], json.loads(Path(manual_groups).read_text()))
    invalid_groups = {r.get("declared_group") for r in audit if "normalized_rgb_sha256" not in r}
    invalid_manual = {g for r in audit if "normalized_rgb_sha256" not in r for g in r.get("manual_review_groups", [])}
    clusters, _ = duplicate_clusters([*protected, *records])
    candidates = []
    for cluster in clusters:
        reviews = [r for r in cluster if r["role"] == "review"]
        if not reviews:
            continue
        group_id = "silver-image-cluster-" + min(r["sha256"] for r in cluster)
        blocked = any(r["role"] == "protected" for r in cluster)
        signatures = {json.dumps(r["input"].get("candidate_labels"), sort_keys=True) for r in reviews}
        for record in reviews:
            record["cluster_id"] = group_id
            if blocked:
                record["reasons"].append("matches_frozen_gold_or_prior_holdout")
            if len(signatures) > 1:
                record["reasons"].append("duplicate_label_conflict_or_unknown")
            if record.get("declared_group") in invalid_groups or set(record.get("manual_review_groups", [])) & invalid_manual:
                record["reasons"].append("related_group_has_unverified_image")
        eligible = sorted((r for r in reviews if not r["reasons"]), key=lambda r: r["id"])
        for duplicate in eligible[1:]:
            duplicate["reasons"].append("duplicate_or_related_source_group")
        if not eligible:
            continue
        record = eligible[0]; src = record["source"]; row = record["input"]
        candidates.append(dict(id=src["id"], image=src["resolved_image"], image_sha256=record["sha256"],
            source=SOURCE, license=src["license"], license_sha256=src["license_sha256"],
            source_object_generation=src["object_generation"], source_group=src["group_id"],
            subject_id=group_id, subject_id_verified=False, image_scope=row["image_scope"],
            domain_stratum="other_body_region" if "other_body_region" in row["quality_flags"] else row["image_scope"],
            candidate_labels=dict(row["candidate_labels"]), source_review_sha256=row["source_review_sha256"],
            label_origin="ai_visual_review", human_expert_verified=False, training_eligible=False,
            research_only=True, admission="requires_separate_validated_silver_intake", quality_flags=row["quality_flags"],
            cleaning_features={key: record[key] for key in FINGERPRINTS}))
    for record in audit:
        record["status"] = "quarantined" if record["reasons"] else "clean_candidate"
    after = {path: digest(path) for path in before}
    if before != after:
        raise ValueError("Input artifacts changed during cleaning")
    summary = dict(input_records=len(audit), clean_candidates=len(candidates), training_eligible=0,
        human_expert_gold=0, protected_records=len(protected), no_protected_images_opened=True,
        counts_by_stratum=dict(collections.Counter(r["domain_stratum"] for r in candidates)),
        positive_tag_counts={tag: sum(r["candidate_labels"][tag] for r in candidates) for tag in sorted(TAGS)},
        reason_counts=dict(collections.Counter(reason for r in audit for reason in set(r["reasons"]))),
        input_sha256=before, inputs_unchanged=True, frozen_splits_modified=False,
        code_sha256={str(Path(__file__).resolve()): digest(__file__),
                     str(ROOT / "research/clean_expansion.py"): digest(ROOT / "research/clean_expansion.py")},
        gold_or_test_labels_generated=False, automatic_deployment=False)
    output.mkdir(parents=True, exist_ok=False)
    for name, values in (("candidates.jsonl", candidates), ("protected-index.jsonl", protected),
                         ("audit.jsonl", [{k: r[k] for k in ("id", "sha256", "status", "reasons", "cluster_id") if k in r} for r in audit])):
        (output / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in values))
    (output / "clean-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("queue", "protection-audit", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("source-queue", "ai-review", "ocr"):
        parser.add_argument("--" + name, type=Path, action="append", required=True)
    parser.add_argument("--manual-groups", type=Path)
    parser.add_argument("--path-map", action="append", default=[])
    args = parser.parse_args()
    maps = [value.split("=", 1) for value in args.path_map]
    result = clean(args.queue, args.source_queue, args.ai_review, args.ocr, args.protection_audit,
                   args.output, maps, args.manual_groups)
    print(json.dumps({k: result[k] for k in ("input_records", "clean_candidates", "training_eligible",
                                             "counts_by_stratum", "reason_counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
