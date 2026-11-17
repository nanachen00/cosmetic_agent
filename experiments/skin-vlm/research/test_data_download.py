import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.download_scin import fetch_object, review_bucket, select_cases, source_yes


class ScinTests(unittest.TestCase):
    def test_actual_source_yes_values(self):
        self.assertTrue(source_yes("YES"))
        self.assertFalse(source_yes(""))
        self.assertFalse(source_yes("NO"))

    def test_case_sampling_is_order_independent_and_does_not_duplicate_cases(self):
        cases = [{"case_id": str(i), "body_parts_head_or_neck": "YES", "image_1_path": f"dataset/images/{i}.png"}
                 for i in range(12)]
        labels = {str(i): {"weighted_skin_condition_label": "{'Acne': 1.0}",
                           "monk_skin_tone_label_us": str(i % 3 + 1)} for i in range(12)}
        first = select_cases(cases, labels, 8)
        second = select_cases(list(reversed(cases)), labels, 8)
        ids = [row[0]["case_id"] for row in first]
        self.assertEqual(ids, [row[0]["case_id"] for row in second])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({row[3] for row in first}, {"1", "2", "3"})

    def test_flaking_review_bucket_uses_source_yes(self):
        self.assertEqual(review_bucket({"textures_rough_or_flaky": "YES"}, {}), "flaking")

    def test_cached_image_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "dataset/images/a.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupted")
            metadata = {"generation": "1", "size": "9", "md5Hash": "not-the-checksum"}
            with patch("research.download_scin.get", return_value=json.dumps(metadata).encode()):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    fetch_object("dataset/images/a.png", Path(folder))


if __name__ == "__main__":
    unittest.main()
