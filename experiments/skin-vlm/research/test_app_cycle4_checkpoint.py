"""Real CPU optimizer round trips; CUDA states are mocked CPU byte tensors."""
import errno
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import torch
from safetensors.torch import load_file, save_file

try:
    from research import app_cycle4_checkpoint as checkpoint
except ImportError:
    import app_cycle4_checkpoint as checkpoint


class TinyAdapter(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(2, 1)

    def save_pretrained(self, directory, *, safe_serialization):
        assert safe_serialization is True
        folder = Path(directory)
        save_file({name: value.detach().cpu().contiguous() for name, value in self.state_dict().items()},
                  folder / "adapter_model.safetensors")
        (folder / "adapter_config.json").write_text('{"test_only":true}')


class TinyProcessor:
    def save_pretrained(self, directory, *, safe_serialization):
        assert safe_serialization is True
        (Path(directory) / "processor_config.json").write_text('{"test_only":true}')


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        # Resolve macOS /var aliases. TMPDIR can point to an NFS scratch root.
        self.directory = tempfile.TemporaryDirectory(prefix="cycle4-checkpoint-cpu-")
        self.root = Path(self.directory.name).resolve() / "checkpoints"
        self.model = TinyAdapter()
        self.processor = TinyProcessor()
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=0.01)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=1, gamma=0.8)
        self.rng = random.Random(42)
        self.fingerprint = "test-protocol-v1"
        self.state = dict(global_step=1, epoch_index=0, next_offset=3, order=[3, 1, 0, 2],
                          history=[{"loss": 0.5}], config={"lr": 0.01, "seed": 42})
        self.step(self.model, self.optimizer, self.scheduler)

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def step(model, optimizer, scheduler):
        optimizer.zero_grad()
        loss = model.layer(torch.tensor([[0.2, 0.8], [0.5, 0.3]])).square().mean()
        loss.backward()
        optimizer.step()
        scheduler.step()

    def save(self, **changes):
        return checkpoint.save_checkpoint(self.root, self.model, self.processor, self.optimizer,
            self.scheduler, dict(self.state, **changes), self.fingerprint, self.rng)

    def test_actual_adamw_scheduler_and_rng_roundtrip_resumes_next_step(self):
        path = self.save()
        original_load = torch.load
        with patch.object(torch, "load", wraps=original_load) as load:
            loaded = checkpoint.load_checkpoint(path, self.fingerprint)
        self.assertEqual(load.call_args.kwargs, {"weights_only": True, "map_location": "cpu"})
        self.assertEqual(set(loaded), {"optimizer", "scheduler", "torch_rng", "cuda_rng", "python_random_state", "state"})
        self.assertEqual(loaded["state"], self.state)
        self.assertEqual(loaded["python_random_state"], self.rng.getstate())
        self.assertEqual(loaded["torch_rng"].device.type, "cpu")
        expected_random = torch.rand(5)
        expected_python = self.rng.random()
        self.step(self.model, self.optimizer, self.scheduler)
        resumed = TinyAdapter()
        resumed.load_state_dict(load_file(str(path / "adapter_model.safetensors")))
        optimizer = torch.optim.AdamW(resumed.parameters(), lr=7.0)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.8)
        optimizer.load_state_dict(loaded["optimizer"])
        scheduler.load_state_dict(loaded["scheduler"])
        torch.set_rng_state(loaded["torch_rng"])
        self.assertTrue(torch.equal(torch.rand(5), expected_random))
        rng = random.Random()
        rng.setstate(loaded["python_random_state"])
        self.assertEqual(rng.random(), expected_python)
        self.step(resumed, optimizer, scheduler)
        for name, value in self.model.state_dict().items():
            self.assertTrue(torch.equal(value, resumed.state_dict()[name]))
        self.assertEqual(scheduler.state_dict(), self.scheduler.state_dict())

    def test_step_zero_and_nonmultiples_of_25_are_allowed(self):
        for step in [0, 7, 25]:
            path = self.save(global_step=step)
            self.assertEqual(path.name, f"step-{step:06d}")
            self.assertEqual(checkpoint.load_checkpoint(path, self.fingerprint)["state"]["global_step"], step)

    def test_cuda_rng_can_roundtrip_without_initializing_a_gpu(self):
        fake = [torch.arange(12, dtype=torch.uint8), torch.arange(8, dtype=torch.uint8)]
        with patch.object(torch.cuda, "is_initialized", return_value=True), patch.object(torch.cuda, "get_rng_state_all", return_value=fake):
            path = self.save()
        values = checkpoint.load_checkpoint(path, self.fingerprint)["cuda_rng"]
        self.assertEqual(len(values), 2)
        self.assertTrue(all(torch.equal(a, b) and a.device.type == "cpu" for a, b in zip(values, fake)))

    def test_fingerprint_mismatch_is_rejected_before_torch_load(self):
        path = self.save()
        with patch.object(torch, "load") as load, self.assertRaisesRegex(ValueError, "fingerprint"):
            checkpoint.load_checkpoint(path, "other-protocol")
        load.assert_not_called()

    def test_corruption_missing_and_extra_files_are_rejected_before_torch_load(self):
        path = self.save()
        for change in ["corrupt", "missing", "extra"]:
            victim = path / "adapter_config.json"
            original = victim.read_bytes()
            if change == "corrupt":
                victim.write_bytes(b"corrupted")
            elif change == "missing":
                victim.unlink()
            else:
                (path / "extra.json").write_text("{}")
            with patch.object(torch, "load") as load, self.assertRaisesRegex(ValueError, "SHA-256"):
                checkpoint.load_checkpoint(path, self.fingerprint)
            load.assert_not_called()
            victim.write_bytes(original)
            (path / "extra.json").unlink(missing_ok=True)

    def test_overwrite_and_partial_reuse_are_refused(self):
        path = self.save()
        original = (path / checkpoint.MANIFEST).read_bytes()
        with self.assertRaises(FileExistsError):
            self.save()
        self.assertEqual((path / checkpoint.MANIFEST).read_bytes(), original)
        (self.root / "step-000002.partial").mkdir()
        with self.assertRaises(FileExistsError):
            self.save(global_step=2)
        with self.assertRaisesRegex(ValueError, "partial"):
            checkpoint.load_checkpoint(self.root / "step-000002.partial", self.fingerprint)

    def test_native_publish_cannot_replace_an_empty_racing_destination(self):
        source = self.root.parent / "pending"
        target = self.root.parent / "already-present"
        source.mkdir()
        target.mkdir()
        with self.assertRaises(FileExistsError):
            checkpoint._rename_without_replacement(source, target)
        self.assertTrue(source.is_dir() and target.is_dir())

    def test_nfs_unsupported_native_publish_uses_exclusive_manifest_last_commit(self):
        with patch.object(checkpoint, "_native_rename_without_replacement", side_effect=OSError(errno.EINVAL, "NFS unsupported")):
            path = self.save()
        self.assertEqual(checkpoint.load_checkpoint(path, self.fingerprint)["state"], self.state)
        self.assertFalse(path.with_name(path.name + ".partial").exists())
        source = self.root.parent / "other-partial"
        source.mkdir()
        (source / checkpoint.MANIFEST).write_text('{"files":{}}')
        existing = self.root.parent / "reserved"
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            checkpoint._publish_reserved(source, existing)
        self.assertTrue(source.is_dir() and existing.is_dir())

    def test_interrupted_manifest_commit_is_never_loadable_or_overwritten(self):
        rename = os.rename
        def fail_commit(source, target):
            if Path(source).name == checkpoint.MANIFEST:
                raise OSError("simulated interrupted publication")
            return rename(source, target)
        with patch.object(checkpoint, "_native_rename_without_replacement", side_effect=OSError(errno.EINVAL, "NFS unsupported")), patch.object(checkpoint.os, "rename", side_effect=fail_commit), self.assertRaisesRegex(OSError, "interrupted"):
            self.save()
        target = self.root / "step-000001"
        self.assertTrue(target.is_dir())
        self.assertFalse((target / checkpoint.MANIFEST).exists())
        with patch.object(torch, "load") as load, self.assertRaisesRegex(ValueError, "incomplete"):
            checkpoint.load_checkpoint(target, self.fingerprint)
        load.assert_not_called()
        with self.assertRaises(FileExistsError):
            self.save()

    def test_symlink_and_traversal_paths_and_manifest_members_are_refused(self):
        path = self.save()
        alias = self.root.parent / "alias"
        alias.symlink_to(path, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            checkpoint.load_checkpoint(alias, self.fingerprint)
        with self.assertRaisesRegex(ValueError, "traversal"):
            checkpoint.load_checkpoint(self.root / ".." / "checkpoints" / path.name, self.fingerprint)
        config = path / "adapter_config.json"
        config.unlink()
        config.symlink_to(path / "processor_config.json")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            checkpoint.load_checkpoint(path, self.fingerprint)
        config.unlink()
        config.write_text('{"test_only":true}')
        manifest = json.loads((path / checkpoint.MANIFEST).read_text())
        manifest["files"]["../escape"] = "a" * 64
        (path / checkpoint.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            checkpoint.load_checkpoint(path, self.fingerprint)

    def test_custom_optimizer_objects_are_never_serialized(self):
        with patch.object(self.optimizer, "state_dict", return_value={"unsafe": object()}), self.assertRaisesRegex(TypeError, "Unsafe/custom"):
            self.save()
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
