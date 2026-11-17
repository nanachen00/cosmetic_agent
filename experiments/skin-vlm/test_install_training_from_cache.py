"""CPU-only fail-closed checks for offline training-environment preparation."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import install_training_from_cache as install


class OfflineTrainingInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def wheel(self, name="example", version="1.0"):
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        path = cache / (name + "-" + version + "-py3-none-any.whl")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name + "-" + version + ".dist-info/METADATA", "Name: " + name + "\nVersion: " + version + "\n")
        return path

    def test_real_approved_locks_have_exact_42_plus3_packages(self):
        pure, native = install.read_approved(
            install.HERE / "requirements-photo-linux-py311.lock",
            install.HERE / "requirements-photo-native-rocm71-py311.lock",
        )
        self.assertEqual(len(pure), 42)
        self.assertEqual(len(native), 3)
        self.assertEqual(pure["peft"]["version"], "0.21.3")

    def test_vendored_metadata_in_pip_does_not_replace_top_level_package(self):
        wheel = self.wheel()
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("example/_vendor/nested-2.0.dist-info/METADATA", "Name: nested\nVersion: 2.0\n")
        self.assertEqual(install.wheel_metadata(wheel), ("example", "1.0"))

    def test_changed_approved_lock_is_rejected(self):
        lock = self.root / "tampered.lock"
        lock.write_bytes((install.HERE / "requirements-photo-linux-py311.lock").read_bytes() + b"# changed\n")
        with self.assertRaisesRegex(ValueError, "lock changed"):
            install.lock_lines(lock, install.APPROVED_PURE_SHA)

    def test_wrong_mount_source_ro_or_plain_directory_is_rejected(self):
        good = dict(target=str(self.root), source=install.EXPECTED_NFS, fstype="nfs4", options="rw,relatime")
        install.require_mount(good, self.root)
        for changed in (dict(source="other:/volume"), dict(options="ro"), dict(target="/"), dict(fstype="ext4")):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                install.require_mount(good | changed, self.root)

    def test_nonempty_runtime_is_not_modified(self):
        target = self.root / "blink-runtime/envs/serving"
        target.mkdir(parents=True)
        marker = target / "do-not-modify"
        marker.write_text("still serving")
        with self.assertRaises(FileExistsError):
            install.require_fresh_target(target, self.root)
        self.assertEqual(marker.read_text(), "still serving")

    def test_target_symlink_or_wrong_directory_is_rejected(self):
        target = self.root / "blink-runtime/envs/new"
        target.parent.mkdir(parents=True)
        other = self.root / "elsewhere"
        other.mkdir()
        target.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            install.require_fresh_target(target, self.root)
        with self.assertRaises(ValueError):
            install.require_fresh_target(other, self.root)

    def test_pure_cached_wheel_must_match_approved_hash(self):
        wheel = self.wheel()
        pins = {"example": {"version": "1.0", "sha256": "0" * 64}}
        with self.assertRaisesRegex(ValueError, "approved hash/version"):
            install.verify_pure_cache(wheel.parent, pins, self.root)

    def test_incomplete_pure_cache_is_rejected(self):
        cache = self.root / "empty-cache"
        cache.mkdir()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            install.verify_pure_cache(cache, {"example": {"version": "1.0", "sha256": "0" * 64}}, self.root)

    def test_native_local_hash_cannot_override_approved_hash(self):
        wheel = self.wheel("torch", "2.13.0+rocm7.1")
        lock = self.root / "native-local.lock"
        lock.write_text(str(wheel) + " --hash=sha256:" + install.sha(wheel) + "\n")
        with self.assertRaisesRegex(ValueError, "approved upstream lock"):
            install.verify_native_cache(lock, wheel.parent, {wheel.name: "0" * 64}, self.root)

    def test_native_url_cannot_reach_pip(self):
        lock = self.root / "network.lock"
        lock.write_text("https://download.pytorch.org/model.whl --hash=sha256:" + "0" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "local absolute wheel paths"):
            install.verify_native_cache(lock, self.root, {}, self.root)

    def test_pip_commands_are_local_noindex_nodeps_and_hash_required(self):
        commands = install.install_commands(self.root / "bin/python", self.root, self.root / "native.lock", self.root / "pure.lock", self.root / "cache")
        for command in commands:
            self.assertIn("--no-index", command)
            self.assertIn("--no-deps", command)
            self.assertIn("--require-hashes", command)
            self.assertFalse(any(value.startswith(("http:", "https:")) for value in command))
            self.assertNotIn("download", command)

    def test_child_environment_does_not_inherit_index_credentials_or_torch_path(self):
        with patch.dict(os.environ, {"PIP_INDEX_URL": "private-test-index", "OPENAI_API_KEY": "dummy", "LD_LIBRARY_PATH": "old-torch"}):
            env = install.clean_environment()
        for name in ("PIP_INDEX_URL", "OPENAI_API_KEY", "HF_TOKEN", "LD_LIBRARY_PATH", "PYTHONPATH"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)


if __name__ == "__main__":
    unittest.main()
