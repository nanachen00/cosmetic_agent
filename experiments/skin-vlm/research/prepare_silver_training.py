"""Explicit low-weight silver research intake; never modify or create gold.

Public external-cohort protection uses existing fingerprint metadata only.
This command does not open protected images, evaluate a model, or start training.
"""
from __future__ import annotations
import argparse
import base64
import collections
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import validate
from research.clean_expansion import duplicate_clusters, resolve_image, image_features
from research.clean_silver_candidates import digest, rows, load_sources, load_reviews, FINGERPRINTS, TAGS
from research.download_skincon import target_for

CATEGORIES = ["frozen_train", "frozen_val", "sealed_test", "acne_parent", "external_phone", "scin_gold"]
NFS_TRAINING = Path("/mnt/goodagents/data/training")


def write_rows(path, records):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))


def prepare(clean_dir, source_queues, ai_reviews, review_protocol, fingerprint_audit,
            external_cohort, output, path_maps=(), *, final_visual_review, final_visual_sha256):
    clean_dir, output = Path(clean_dir), Path(output)
    if output.exists():
        raise FileExistsError("Use a fresh research intake directory")
    final_bytes = Path(final_visual_review).read_bytes()
    if (not isinstance(final_visual_sha256, str) or not re.fullmatch("[a-f0-9]{64}", final_visual_sha256)
            or hashlib.sha256(final_bytes).hexdigest() != final_visual_sha256):
        raise ValueError("Final visual review SHA256 differs")
    final_document = json.loads(final_bytes)
    final_rows = final_document.get("reviews") if isinstance(final_document, dict) else None
    if not isinstance(final_rows, list):
        raise ValueError("Final visual review records are required")
    final_by_id = {}
    for row in final_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                or row["id"] in final_by_id or not isinstance(row.get("image_sha256"), str)
                or not re.fullmatch("[a-f0-9]{64}", row["image_sha256"])):
            raise ValueError("Final visual review identity is invalid or ambiguous")
        final_by_id[row["id"]] = row
    summary_path = clean_dir / "clean-summary.json"
    summary = json.loads(summary_path.read_text())
    if not (summary.get("inputs_unchanged") is True and summary.get("frozen_splits_modified") is False
            and summary.get("no_protected_images_opened") is True and summary.get("training_eligible") == 0):
        raise ValueError("Complete AI-only cleaning audit required")
    for path, expected in {**summary["input_sha256"], **summary["code_sha256"]}.items():
        if digest(path) != expected:
            raise ValueError("Upstream cleaning artifact changed")
    if str(Path(fingerprint_audit).resolve()) not in summary["input_sha256"]:
        raise ValueError("Fingerprint audit is not bound to cleaning inputs")
    sources, _ = load_sources(source_queues)
    evidence = load_reviews(ai_reviews)
    source_protocol_sha = digest(review_protocol)
    source_audit = rows(fingerprint_audit)
    by_hash = collections.defaultdict(list)
    for row in source_audit:
        if all(key in row for key in FINGERPRINTS):
            by_hash[row["sha256"]].append(row)

    def decorate(row):
        result = dict(row)
        groups = set(result.get("manual_review_groups", []))
        for original in by_hash.get(row["sha256"], []):
            groups.update(original.get("manual_review_groups", []))
            if original.get("cluster_id"):
                groups.add("prior-audit-cluster:" + original["cluster_id"])
        result["manual_review_groups"] = sorted(groups)
        return result

    exclusion = [decorate(dict(row, role="protected")) for row in rows(clean_dir / "protected-index.jsonl")]
    cohort = json.loads(Path(external_cohort).read_text())
    external = cohort.get("rows")
    if not isinstance(external, list) or not external:
        raise ValueError("External cohort metadata missing")
    for member in external:
        matches = by_hash.get(member.get("original_sha256"), [])
        if not matches:
            raise ValueError("External original fingerprint missing; do not open its image")
        for match in matches:
            record = {key: match[key] for key in ("id", "sha256", *FINGERPRINTS,
                                                 "declared_group", "manual_review_groups") if key in match}
            exclusion.append(decorate(dict(record, role="protected", reserved_category="external_phone")))
    candidates = rows(clean_dir / "candidates.jsonl")
    if len(candidates) != summary["clean_candidates"]:
        raise ValueError("Candidate count differs from cleaning audit")
    proposed, excluded = [], []
    for row in candidates:
        if row.get("domain_stratum") == "other_body_region" or not any(
                row.get("candidate_labels", {}).get(tag) is True for tag in ("dark_marks", "flaking")):
            continue
        if (row.get("label_origin") != "ai_visual_review" or row.get("human_expert_verified") is not False
                or row.get("training_eligible") is not False or row.get("research_only") is not True
                or row.get("image_scope") not in {"face", "skin_closeup"} or row.get("quality_flags")
                or set(row.get("candidate_labels", {})) != TAGS
                or any(type(value) is not bool for value in row["candidate_labels"].values())):
            raise ValueError("Unsafe proposed silver candidate")
        review = evidence.get((row["source_review_sha256"], row["image_sha256"], row["source_group"]))
        if (review is None or review.get("tag_decisions", review.get("candidate_labels")) != row["candidate_labels"]
                or review.get("image_scope") != row["image_scope"] or review.get("silver_candidate") is not True
                or review.get("human_expert_verified") is not False or review.get("training_eligible") is not False):
            raise ValueError("Silver candidate is not bound to original AI decisions")
        record = decorate(dict(id=row["id"], sha256=row["image_sha256"], role="candidate",
            declared_group=row["source_group"], candidate=row, **row["cleaning_features"]))
        proposed.append(record)
    accepted = []
    for cluster in duplicate_clusters([*exclusion, *proposed])[0]:
        silver = [row for row in cluster if row["role"] == "candidate"]
        if any(row["role"] == "protected" for row in cluster):
            excluded.extend(dict(id=row["id"], reason="reserved_exact_near_mirror_or_source_group") for row in silver)
        else:
            if len(silver) > 1:
                raise ValueError("Cleaned candidates contain a duplicate source cluster")
            accepted.extend(row["candidate"] for row in silver)
    admitted = []
    final_excluded = []
    for row in accepted:
        final = final_by_id.get(row["id"])
        reasons = []
        if final is None or final.get("image_sha256") != row["image_sha256"]:
            reasons.append("final_visual_identity_missing_or_changed")
        else:
            features = final.get("features")
            if not isinstance(features, dict) or set(features) != TAGS or any(type(v) is not bool for v in features.values()):
                reasons.append("final_visual_unknown_or_invalid_features")
            elif features != row["candidate_labels"]:
                reasons.append("final_visual_labels_disagree")
            if final.get("image_scope") != row["image_scope"]:
                reasons.append("final_visual_scope_disagrees")
            if final.get("quality_flags") != []:
                reasons.append("final_visual_quality_not_clear")
            if (final.get("silver_candidate") is not True or final.get("human_expert_verified") is not False
                    or final.get("reviewer_type") not in {"codex_visual_assistant", "conversation_assistant"}
                    or final.get("model_version") != "unavailable"):
                reasons.append("final_visual_AI_admission_not_confirmed")
        if reasons:
            final_excluded.append(dict(id=row["id"], image_sha256=row["image_sha256"], reasons=reasons))
        else:
            admitted.append(row)
    accepted = admitted
    maps = [(Path(remote).resolve(), Path(local).resolve()) for remote, local in path_maps]
    manifest = []
    md5_checks = []
    for row in sorted(accepted, key=lambda value: value["id"]):
        source = sources[(row["image_sha256"], row["source_group"])]
        local = resolve_image(row["image"], clean_dir / "candidates.jsonl", maps)
        if local != Path(source["resolved_image"]) or digest(local) != row["image_sha256"]:
            raise ValueError("Silver image bytes or source path changed")
        fresh = image_features(local, row["image_sha256"])
        if fresh["quality_flags"] or any(fresh[key] != row["cleaning_features"][key] for key in FINGERPRINTS):
            raise ValueError("Silver decoded quality or cleaning fingerprints changed")
        remote = next((a / local.relative_to(b) for a, b in maps if local.is_relative_to(b)), local)
        if not remote.is_relative_to(NFS_TRAINING):
            raise ValueError("Silver training paths must be persistent NFS paths")
        provenance = json.loads((Path(source["source_manifest"]).parent / "provenance.json").read_text())
        publisher = next(item for item in provenance["files"] if item["object"] == source["image"])
        md5_bytes = hashlib.md5(local.read_bytes()).digest()
        if publisher.get("md5_base64") is not None:
            if base64.b64decode(publisher["md5_base64"], validate=True) != md5_bytes:
                raise ValueError("Publisher MD5 differs from source bytes")
            md5_status = "verified_against_publisher_gcs_metadata"
        else:
            md5_status = "unavailable_in_publisher_metadata"
        target = target_for(sorted(tag for tag, value in row["candidate_labels"].items() if value))
        target["image_scope"] = row["image_scope"]
        validate(target)
        record = dict(id=row["id"], subject_id=row["subject_id"], image=str(remote),
            image_sha256=row["image_sha256"], source=row["source"], license=row["license"],
            usage_permitted=True, usage_scope="research_only", target=target, note="",
            label_origin="ai_silver", silver_only_train=True, loss_weight=.25, subject_id_verified=False,
            human_expert_verified=False, research_only=True, gold_eligible=False, training_eligible=False,
            source_group=row["source_group"], source_review_sha256=row["source_review_sha256"],
            source_object_generation=source["object_generation"], license_sha256=source["license_sha256"],
            teacher=dict(reviewer_type="conversation_assistant", model_version="unavailable",
                review_protocol_sha256=source_protocol_sha, decisions_sha256=row["source_review_sha256"],
                decisions_artifact_kind="compiled_ai_review_jsonl", second_review_claimed=False),
            source_md5_status=md5_status)
        if publisher.get("md5_base64") is not None:
            record.update(source_image_md5=md5_bytes.hex(), publisher_md5_base64=publisher["md5_base64"])
        manifest.append(record); md5_checks.append(md5_status)
    split_summary_path = Path(fingerprint_audit).parent / "split_info.json"
    split_summary = json.loads(split_summary_path.read_text())
    coverage = {category: dict(status="checked", records=int(split_summary["counts"][split]["images"]))
                for category, split in (("frozen_train", "train"), ("frozen_val", "val"), ("sealed_test", "test"))}
    gold = [row for row in source_audit if row.get("role") in {"existing", "new"} and row.get("status") == "accepted"]
    protected_keys = {(row["id"], row["sha256"]) for row in exclusion}
    if len(gold) != sum(row["records"] for row in coverage.values()) or any(
            (row["id"], row["sha256"]) not in protected_keys for row in gold):
        raise ValueError("Frozen gold coverage is incomplete")
    has_parent_manifest = any("acne04-protected-parents" in row.get("path", "")
                              for row in split_summary.get("input_manifests", []) if row.get("role") == "protected")
    acne_count = sum(row.get("role") == "protected" and "acne04" in row.get("image", "").lower() for row in source_audit)
    coverage["acne_parent"] = dict(status="checked" if has_parent_manifest and acne_count else "missing", records=acne_count)
    coverage["external_phone"] = dict(status="checked", records=len(external), reason="Original-image SHA and precomputed source fingerprints; no protected image opened")
    coverage["scin_gold"] = dict(status="not_applicable", records=0, reason="No human/expert-verified SCIN gold exists in this intake")
    if any(row["status"] == "missing" for row in coverage.values()):
        raise ValueError("Reserved source coverage incomplete")
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = output / "silver_only_train.jsonl"
    index_path = output / "exclusion-index.jsonl"
    write_rows(manifest_path, manifest); write_rows(index_path, exclusion)
    (output / "final-visual-review.json").write_bytes(final_bytes)
    audit = dict(manifest_sha256=digest(manifest_path), silver_only_train=True,
        accepted_ids=[row["id"] for row in manifest], accepted_count=len(manifest),
        training_launch_allowed=bool(manifest),
        status="ready_for_explicit_silver_research" if manifest else "quarantined_no_eligible_silver",
        proposed_target_positive_count=len(proposed), excluded=excluded, loss_weight=.25,
        final_visual_review=dict(path="final-visual-review.json", sha256=final_visual_sha256),
        final_visual_quarantined=final_excluded,
        exclusion_index_sha256=digest(index_path), reserved_categories=CATEGORIES, reserved_coverage=coverage,
        cleaning_checks={key: True for key in ("sha256", "full_decode", "quality", "exact_duplicate", "near_duplicate",
                                               "mirrored_duplicate", "ocr", "holdout_exclusion")},
        cleaning_summary_sha256=digest(summary_path), cleaned_candidates_sha256=digest(clean_dir / "candidates.jsonl"),
        original_protected_index_sha256=digest(clean_dir / "protected-index.jsonl"),
        external_cohort_metadata_sha256=digest(external_cohort), fingerprint_audit_sha256=digest(fingerprint_audit),
        reserved_split_summary_sha256=digest(split_summary_path),
        review_artifacts=[dict(review_protocol_sha256=source_protocol_sha, decisions_sha256=key)
                          for key in sorted({row["candidate"]["source_review_sha256"] for row in proposed})],
        target_template_code_sha256=digest(ROOT / "research/download_skincon.py"),
        manifest_preparer_code_sha256=digest(__file__),
        md5_status_reason=("Publisher GCS MD5 was checked where present; SHA256 and fixed GCS generation always checked"
                           if manifest else "No image admitted after final visual gate; no new MD5 checks claimed for empty intake"),
        md5_counts=dict(collections.Counter(md5_checks)), teacher_model_version="unavailable",
        independent_human_gold=False, frozen_splits_modified=False, protected_images_opened=False,
        automatic_deployment=False)
    audit["cleaning_checks"]["md5"] = True if md5_checks and all(value.startswith("verified_") for value in md5_checks) else "unavailable"
    (output / "silver-intake-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    (output / "quarantine-intake-report.json").write_text(json.dumps(dict(
        accepted_count=len(manifest), training_launch_allowed=bool(manifest),
        final_visual_review_sha256=final_visual_sha256, quarantined=final_excluded,
        reserved_exclusions=excluded, original_gold_or_silver_inputs_modified=False,
        human_expert_gold_created=False), ensure_ascii=False, indent=2) + "\n")
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("clean-dir", "review-protocol", "fingerprint-audit", "external-cohort", "output", "final-visual-review"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("source-queue", "ai-review"):
        parser.add_argument("--" + name, type=Path, action="append", required=True)
    parser.add_argument("--path-map", action="append", default=[])
    parser.add_argument("--final-visual-review-sha256", required=True)
    args = parser.parse_args()
    result = prepare(args.clean_dir, args.source_queue, args.ai_review, args.review_protocol,
        args.fingerprint_audit, args.external_cohort, args.output, [value.split("=", 1) for value in args.path_map],
        final_visual_review=args.final_visual_review, final_visual_sha256=args.final_visual_review_sha256)
    print(json.dumps({key: result[key] for key in ("accepted_count", "proposed_target_positive_count", "manifest_sha256",
                                                  "exclusion_index_sha256", "loss_weight", "md5_counts")}))


if __name__ == "__main__":
    main()
