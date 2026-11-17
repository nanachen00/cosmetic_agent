"""CPU-only silver provenance, image, OCR and protected-group checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageOps

from research.clean_expansion import image_features
from research.clean_silver_candidates import clean, digest, SOURCE, TAGS


class SilverCleaningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.image = self.source / "dataset/fixture.png"
        self.image.parent.mkdir(parents=True)
        self.make_image(self.image, 17)
        self.sha = digest(self.image)
        self.license = self.source / "SCIN-LICENSE.txt"
        self.license.write_text("offline fixture license")
        self.provenance = self.source / "provenance.json"
        self.provenance.write_text(json.dumps(dict(source=SOURCE, license_sha256=digest(self.license),
            files=[dict(object="dataset/fixture.png", sha256=self.sha, generation="123")])) )
        self.source_queue = self.source / "review_queue.jsonl"
        self.source_row = dict(id="scin-fixture", image="dataset/fixture.png", sha256=self.sha,
            group_id="scin-case-fixture", source=SOURCE, license="SCIN Data Use License", license_record="SCIN-LICENSE.txt")
        self.write_rows(self.source_queue, [self.source_row])
        labels = {key: key == "dark_marks" for key in TAGS}
        self.review = dict(id="scin-fixture", image_sha256=self.sha, source_group="scin-case-fixture",
            tag_decisions=labels, image_scope="face", quality_flags=[], silver_candidate=True,
            label_origin="ai_visual_review", human_expert_verified=False, training_eligible=False)
        self.evidence = self.root / "ai-review.jsonl"
        self.write_rows(self.evidence, [self.review])
        self.intake = dict(image_path_on_gpu=str(self.image), image_sha256=self.sha,
            source_group="scin-case-fixture", image_scope="face", candidate_labels=labels,
            label_origin="ai_visual_review", human_expert_verified=False, training_eligible=False,
            quality_flags=[], existing_silver_candidate=True, source_review_sha256=digest(self.evidence))
        self.queue = self.root / "queue.jsonl"
        self.ocr = self.root / "ocr.jsonl"
        self.write_rows(self.ocr, [dict(sha256=self.sha, complete=True, policy_version=2, condition_token_hits=[])])
        unrelated = self.root / "unrelated.png"
        self.make_image(unrelated, 932)
        self.protected_row = dict(id="sealed-fixture", role="protected", status="protected",
            image="/sealed-do-not-open/test-image.png", **image_features(unrelated, digest(unrelated)))
        self.protection = self.root / "protected-audit.jsonl"
        self.out = self.root / "cleaned"

    def make_image(self, path, seed):
        Image.fromarray(np.random.default_rng(seed).integers(0, 256, (240, 240, 3), dtype=np.uint8)).save(path)

    def write_rows(self, path, records):
        path.write_text("".join(json.dumps(record) + "\n" for record in records))

    def run_clean(self):
        self.write_rows(self.queue, [self.intake])
        self.write_rows(self.protection, [self.protected_row])
        return clean(self.queue, [self.source_queue], [self.evidence], [self.ocr], self.protection, self.out)

    def test_complete_candidate_is_still_ai_only_and_inputs_unchanged(self):
        before = {str(p): digest(p) for p in (self.image, self.source_queue, self.evidence, self.ocr)}
        summary = self.run_clean()
        self.assertEqual(summary["clean_candidates"], 1)
        self.assertEqual(summary["training_eligible"], 0)
        self.assertTrue(summary["no_protected_images_opened"])
        row = json.loads((self.out / "candidates.jsonl").read_text())
        self.assertEqual(row["label_origin"], "ai_visual_review")
        self.assertIs(row["human_expert_verified"], False)
        self.assertIs(row["training_eligible"], False)
        self.assertIs(row["research_only"], True)
        self.assertNotIn("target", row)
        self.assertEqual(before, {path: digest(path) for path in before})
        protected = json.loads((self.out / "protected-index.jsonl").read_text())
        self.assertNotIn("image", protected)

    def test_changed_bytes_fail_checksum(self):
        self.make_image(self.image, 18)
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertEqual(result["reason_counts"]["source_checksum_mismatch"], 1)

    def test_missing_image_and_metadata_never_admit(self):
        self.image.unlink()
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertEqual(result["reason_counts"]["image_missing_or_invalid"], 1)

    def test_unknown_is_not_converted_to_negative(self):
        self.intake["candidate_labels"] = dict(self.intake["candidate_labels"], flaking=None)
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertIn("appearance_labels_missing_or_unknown", result["reason_counts"])

    def test_mirrored_holdout_is_blocked_without_opening_holdout(self):
        path = self.root / "mirrored.png"
        with Image.open(self.image) as image:
            ImageOps.mirror(image).save(path)
        self.protected_row.update(image_features(path, digest(path)))
        path.unlink()
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertEqual(result["reason_counts"]["matches_frozen_gold_or_prior_holdout"], 1)

    def test_same_source_group_is_blocked_even_when_pixels_differ(self):
        self.protected_row["declared_group"] = self.intake["source_group"]
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertEqual(result["reason_counts"]["matches_frozen_gold_or_prior_holdout"], 1)

    def test_existing_gold_train_is_protected_too(self):
        self.protected_row.update(role="existing", status="accepted", declared_group=self.intake["source_group"])
        self.assertEqual(self.run_clean()["clean_candidates"], 0)

    def test_ai_cannot_be_promoted_by_changing_a_flag(self):
        self.intake.update(label_origin="human_verified", human_expert_verified=True, training_eligible=True)
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertEqual(result["human_expert_gold"], 0)

    def test_evidence_hash_and_labels_are_bound(self):
        self.intake["source_review_sha256"] = "0" * 64
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertIn("ai_evidence_unverified_or_not_silver", result["reason_counts"])

    def test_ocr_failure_and_condition_text_are_rejected(self):
        self.write_rows(self.ocr, [dict(sha256=self.sha, complete=True, policy_version=2, condition_token_hits=["fixture"])])
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertIn("condition_text_label_leakage", result["reason_counts"])

    def test_same_ocr_bytes_at_two_paths_are_not_a_policy_conflict(self):
        check = dict(sha256=self.sha, complete=True, policy_version=2, condition_token_hits=[])
        self.write_rows(self.ocr, [dict(check, path="first"), dict(check, path="second")])
        self.assertEqual(self.run_clean()["clean_candidates"], 1)

    def test_source_relative_path_is_resolved_from_original_queue_not_intake(self):
        del self.intake["image_path_on_gpu"]
        self.intake["image_path_from_source_queue"] = self.source_row["image"]
        self.assertEqual(self.run_clean()["clean_candidates"], 1)

    def test_missing_source_license_fails_closed(self):
        self.license.unlink()
        with self.assertRaisesRegex(ValueError, "license"):
            self.run_clean()
        self.assertFalse(self.out.exists())

    def test_incomplete_protection_registry_fails_closed(self):
        del self.protected_row["mirrored_phash63"]
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.run_clean()

    def test_body_region_never_admitted_as_face(self):
        self.intake["quality_flags"] = ["other_body_region"]
        result = self.run_clean()
        self.assertEqual(result["clean_candidates"], 0)
        self.assertIn("face_scope_conflicts_with_body_region", result["reason_counts"])


if __name__ == "__main__":
    unittest.main()
