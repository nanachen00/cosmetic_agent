"""CPU checks for explicit weak-label intake and independent cohort exclusion."""
import base64
import hashlib
import json
from pathlib import Path
import unittest

from research import test_clean_silver_candidates as fixtures
from research.clean_silver_candidates import clean, digest
from research.prepare_silver_training import prepare


class SilverPreparationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SilverCleaningTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        # Test fingerprints include all three frozen splits, with paths that
        # cannot be opened. Only the proposed synthetic silver image is read.
        self.registry = [dict(f.protected_row, image="/sealed/acne04/parent.png")]
        self.registry += [dict(f.protected_row, id="gold-" + split, role="existing", status="accepted")
                          for split in ("train", "val", "test")]
        f.write_rows(f.queue, [f.intake]);f.write_rows(f.protection, self.registry)
        self.split_summary = f.root / "split_info.json"
        self.split_summary.write_text(json.dumps(dict(counts={key: {"images": 1} for key in ("train", "val", "test")},
            input_manifests=[dict(role="protected", path="/sealed/acne04-protected-parents/protected-parents.jsonl")])) )
        provenance = json.loads(f.provenance.read_text())
        provenance["files"][0]["md5_base64"] = base64.b64encode(hashlib.md5(f.image.read_bytes()).digest()).decode()
        f.provenance.write_text(json.dumps(provenance))
        clean(f.queue, [f.source_queue], [f.evidence], [f.ocr], f.protection, f.out)
        self.protocol = f.root / "compiler.py"
        self.protocol.write_text("# offline fixture protocol\n")
        self.external = f.root / "external.json"
        self.external.write_text(json.dumps({"rows": [{"original_sha256": f.protected_row["sha256"]}]}))
        self.final_review = f.root / "final-review.json"
        self.final_row = dict(id=f.intake["source_group"].replace("scin-case-", "scin-"), image_sha256=f.sha,
            image_scope="face", features=f.intake["candidate_labels"], quality_flags=[], silver_candidate=True,
            reviewer_type="codex_visual_assistant", human_expert_verified=False, model_version="unavailable")
        self.final_review.write_text(json.dumps({"reviews": [self.final_row]}))
        self.output = f.root / "intake"

    def run_prepare(self):
        f = self.f
        return prepare(f.out, [f.source_queue], [f.evidence], self.protocol, f.protection,
            self.external, self.output, [("/mnt/goodagents/data/training/fixture", str(f.source))],
            final_visual_review=self.final_review, final_visual_sha256=digest(self.final_review))

    def test_low_weight_ai_only_manifest_and_publisher_md5_are_real(self):
        audit = self.run_prepare()
        row = json.loads((self.output / "silver_only_train.jsonl").read_text())
        self.assertEqual(row["image"], "/mnt/goodagents/data/training/fixture/dataset/fixture.png")
        self.assertEqual(row["label_origin"], "ai_silver")
        self.assertEqual(row["loss_weight"], .25)
        self.assertIs(row["silver_only_train"], True)
        self.assertIs(row["gold_eligible"], False)
        self.assertIs(row["human_expert_verified"], False)
        self.assertEqual(row["source_image_md5"], hashlib.md5(self.f.image.read_bytes()).hexdigest())
        self.assertTrue(audit["cleaning_checks"]["md5"])
        self.assertEqual(row["teacher"]["model_version"], "unavailable")
        self.assertEqual(row["teacher"]["decisions_sha256"], digest(self.f.evidence))
        self.assertEqual(row["teacher"]["review_protocol_sha256"], digest(self.protocol))
        self.assertTrue(all(item["region"] == "unknown" for item in row["target"]["observations"]))
        self.assertEqual(audit["reserved_coverage"]["external_phone"]["status"], "checked")
        self.assertFalse(audit["protected_images_opened"])

    def test_external_exact_overlap_removes_candidate_without_reading_external_image(self):
        # Add an already-computed registry fingerprint, not a protected image.
        from research.clean_expansion import image_features
        record = dict(id="external-only", role="review_pending", **image_features(self.f.image, self.f.sha))
        self.registry.append(record)
        self.f.write_rows(self.f.protection, self.registry)
        clean_summary = json.loads((self.f.out / "clean-summary.json").read_text())
        clean_summary["input_sha256"][str(self.f.protection.resolve())] = digest(self.f.protection)
        (self.f.out / "clean-summary.json").write_text(json.dumps(clean_summary))
        self.external.write_text(json.dumps({"rows": [{"original_sha256": self.f.sha}]}))
        result = self.run_prepare()
        self.assertEqual(result["accepted_count"], 0)
        self.assertFalse(result["training_launch_allowed"])

    def test_missing_external_fingerprint_never_opens_or_ignores_holdout(self):
        self.external.write_text(json.dumps({"rows": [{"original_sha256": "0" * 64}]}))
        with self.assertRaisesRegex(ValueError, "fingerprint missing"):
            self.run_prepare()

    def test_changed_cleaning_input_is_rejected(self):
        self.f.ocr.write_text("changed")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.run_prepare()

    def test_unknown_labels_cannot_be_promoted_by_editing_candidate_file(self):
        path = self.f.out / "candidates.jsonl"
        row = json.loads(path.read_text());row["candidate_labels"]["flaking"] = None
        self.f.write_rows(path, [row])
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.run_prepare()

    def test_redness_only_is_not_added_to_target_positive_research(self):
        path = self.f.out / "candidates.jsonl"
        row = json.loads(path.read_text());row["candidate_labels"] = {key: key == "redness" for key in row["candidate_labels"]}
        self.f.write_rows(path, [row])
        result = self.run_prepare()
        self.assertEqual(result["accepted_count"], 0)
        self.assertFalse(result["training_launch_allowed"])

    def test_unavailable_publisher_md5_is_not_reported_as_verified(self):
        provenance = json.loads(self.f.provenance.read_text());del provenance["files"][0]["md5_base64"]
        self.f.provenance.write_text(json.dumps(provenance))
        summary_path = self.f.out / "clean-summary.json"
        summary = json.loads(summary_path.read_text())
        summary["input_sha256"][str(self.f.provenance.resolve())] = digest(self.f.provenance)
        summary_path.write_text(json.dumps(summary))
        audit = self.run_prepare()
        self.assertEqual(audit["cleaning_checks"]["md5"], "unavailable")
        row = json.loads((self.output / "silver_only_train.jsonl").read_text())
        self.assertNotIn("source_image_md5", row)

    def test_first_silver_true_cannot_override_second_unknown(self):
        self.final_row["features"] = dict(self.final_row["features"], flaking=None)
        self.final_review.write_text(json.dumps({"reviews": [self.final_row]}))
        result = self.run_prepare()
        self.assertEqual(result["accepted_count"], 0)
        self.assertFalse(result["training_launch_allowed"])
        self.assertEqual((self.output / "silver_only_train.jsonl").read_bytes(), b"")
        self.assertIn("final_visual_unknown_or_invalid_features", result["final_visual_quarantined"][0]["reasons"])

    def test_first_silver_true_cannot_override_second_black_mask(self):
        self.final_row["quality_flags"] = ["large_privacy_redaction"]
        self.final_review.write_text(json.dumps({"reviews": [self.final_row]}))
        result = self.run_prepare()
        self.assertEqual(result["accepted_count"], 0)
        self.assertIn("final_visual_quality_not_clear", result["final_visual_quarantined"][0]["reasons"])

    def test_final_review_digest_is_required_and_copied_exactly(self):
        result = self.run_prepare()
        self.assertEqual(result["final_visual_review"]["sha256"], digest(self.final_review))
        self.assertEqual((self.output / "final-visual-review.json").read_bytes(), self.final_review.read_bytes())

    def test_changed_final_review_hash_fails_before_publication(self):
        f = self.f
        with self.assertRaisesRegex(ValueError, "Final visual review SHA256"):
            prepare(f.out, [f.source_queue], [f.evidence], self.protocol, f.protection, self.external,
                self.output, final_visual_review=self.final_review, final_visual_sha256="0" * 64)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
