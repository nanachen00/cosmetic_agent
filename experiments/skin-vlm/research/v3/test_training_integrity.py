import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.v3.prepare_cycle3_data import prior_groups
from research.v3.train_cycle3 import backup_checkpoint, expanded_training_rows, lora_targets, require_persistent_mount, validate_data


class TrainingIntegrityTests(unittest.TestCase):
    def test_plain_folder_cannot_impersonate_persistent_mount(self):
        fake=json.dumps({"filesystems":[{"target":"/","fstype":"overlay"}]})
        with patch("research.v3.train_cycle3.subprocess.check_output",return_value=fake):
            with self.assertRaisesRegex(RuntimeError,"persistent NFS"):
                require_persistent_mount([Path("/mnt/goodagents/checkpoints")])

    def test_corrupted_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/"source";source.mkdir()
            (source/"adapter_model.safetensors").write_bytes(b"adapter-fixture")
            destination=root/"backup"
            backup_checkpoint(source,destination,{"revision":"fixture"})
            (destination/"adapter_model.safetensors").write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError,"backup differs"):
                backup_checkpoint(source,destination,{"revision":"fixture"})

    def make_data(self, root):
        counts = {}
        for split in ("train", "validation"):
            data = split.encode()
            (root / (split + ".png")).write_bytes(data)
            row = {"id": split, "group_id": split, "image": split + ".png", "lesion_present": True,
                   "sha256": hashlib.sha256(data).hexdigest()}
            manifest = root / (split + ".jsonl")
            manifest.write_text(json.dumps(row) + "\n")
            counts[split] = {"sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
        (root / "dataset.json").write_text(json.dumps({"counts": counts}))

    def test_missing_historical_manifest_cannot_silently_reduce_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            present = Path(folder) / "present.jsonl"
            present.write_text('{"group_id":"old-test"}\n')
            with self.assertRaises(FileNotFoundError):
                prior_groups([present, Path(folder) / "missing.jsonl"])

    def test_validates_only_train_validation_without_reading_locked_test(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_data(root)
            (root / "test.jsonl").write_text("not-valid-json; trainer must not read this")
            self.assertEqual(len(validate_data(root)["train"]), 1)

    def test_changed_image_rejected_before_loading_gpu_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_data(root)
            (root / "train.png").write_bytes(b"replaced")
            with self.assertRaisesRegex(ValueError, "image checksum"):
                validate_data(root)

    def test_changed_manifest_rejected_after_freeze(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_data(root)
            path = root / "train.jsonl"
            path.write_text(path.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                validate_data(root)

    def test_visual_comparison_cannot_fall_back_to_language_only(self):
        names = ["model.language_model.layers.0.self_attn.q_proj"]
        with self.assertRaisesRegex(RuntimeError, "merger targets missing"):
            lora_targets(names, "language_attention_mlp_visual_merger")
        names += ["model.visual.merger.linear_fc1", "model.visual.blocks.0.attn.qkv"]
        selected = lora_targets(names, "language_attention_mlp_visual_merger")
        self.assertIn("model.visual.merger.linear_fc1", selected)
        self.assertNotIn("model.visual.blocks.0.attn.qkv", selected)

    def test_hard_positive_variant_requires_actual_training_predictions(self):
        with self.assertRaisesRegex(ValueError, "no training cross-validation"):
            expanded_training_rows([{"id": "one"}], {"hard_positive_sampling": "2x"})


if __name__ == "__main__":
    unittest.main()
