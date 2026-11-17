import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("acquire_targeted_sources", Path(__file__).with_name("acquire_targeted_sources.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def source_file(prefix=1, extension="jpg", data=b"published image bytes"):
    md5 = hashlib.md5(data).hexdigest()
    file_id = prefix * 2 + (extension == "png")
    return {"name": f"{prefix}_F.{extension}", "id": file_id, "size": len(data), "computed_md5": md5,
            "supplied_md5": md5, "download_url": f"https://ndownloader.figshare.com/files/{file_id}", "is_link_only": False}


def metadata():
    return {"id": 29209229, "version": 1, "license": {"url": "https://creativecommons.org/licenses/by/4.0/"},
            "files": [source_file(prefix, extension) for prefix in range(10, 0, -1) for extension in ("png", "jpg")]}


class IntakeTests(unittest.TestCase):
    def test_pinned_source_and_stable_small_selection(self):
        catalog, selected = module.select_files(metadata())
        self.assertEqual(len(catalog), 20)
        self.assertEqual([item["name"] for item in selected[::2]], [f"{prefix}_F.jpg" for prefix in range(1, 9)])
        self.assertEqual(len(selected), 16)
        self.assertEqual(selected[0]["leakage_group"], selected[1]["leakage_group"])
        for field, value in [("version", 2), ("id", 1), ("license", {"url": "unknown"})]:
            changed = metadata(); changed[field] = value
            with self.assertRaises(ValueError): module.select_files(changed)
        with self.assertRaises(ValueError): module.select_files(metadata(), 9)

    def test_bad_paths_urls_md5_and_size_are_rejected(self):
        for field, value in [("name", "../1_F.jpg"), ("name", "/1_F.jpg"), ("download_url", "http://169.254.169.254/"),
                             ("computed_md5", ""), ("supplied_md5", "0" * 32), ("size", module.MAX_FILE_BYTES + 1), ("is_link_only", True)]:
            changed = source_file(); changed[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): module.file_entry(changed)

    def test_missing_pair_and_duplicate_filename_are_rejected(self):
        changed = metadata(); changed["files"] = [item for item in changed["files"] if item["name"] != "1_F.png"]
        with self.assertRaises(ValueError): module.select_files(changed)
        changed = metadata(); changed["files"].append(copy.deepcopy(changed["files"][0]))
        with self.assertRaises(ValueError): module.select_files(changed)

    def test_exact_nfs_mount_required(self):
        expected = {"target": str(module.MOUNT), "source": module.NFS_SOURCE, "fstype": "nfs4", "options": "rw,relatime"}
        module.validate_mount(expected)
        for field, value in [("target", "/"), ("source", "wrong:/volume"), ("fstype", "ext4"), ("options", "ro,relatime")]:
            changed = {**expected, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError): module.validate_mount(changed)

    def test_target_refuses_escape_and_symlinks(self):
        for target in [Path("relative"), module.INTAKE_ROOT, Path("/tmp/intake"), module.INTAKE_ROOT / "../escape"]:
            with self.assertRaises(ValueError): module.verify_target(target)
        with tempfile.TemporaryDirectory() as temporary:
            previous = module.INTAKE_ROOT
            try:
                module.INTAKE_ROOT = Path(temporary).resolve()
                target = module.INTAKE_ROOT / "safe"
                module.verify_target(target)
                target.symlink_to(module.INTAKE_ROOT, target_is_directory=True)
                with self.assertRaises(ValueError): module.verify_target(target)
            finally:
                module.INTAKE_ROOT = previous

    def test_publisher_hash_verified_and_successful_files_never_redownload(self):
        data = b"published image bytes"
        entry = module.file_entry(source_file(data=data))
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(data)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = module.download_one(entry, directory, opener)
            second = module.download_one(entry, directory, opener)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first["sha256"], hashlib.sha256(data).hexdigest())
            self.assertFalse(first["reused_verified_file"])
            self.assertTrue(second["reused_verified_file"])
            self.assertFalse((directory / "1_F.jpg.partial").exists())

    def test_wrong_bytes_never_become_completed(self):
        entry = module.file_entry(source_file())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaises(ValueError): module.download_one(entry, directory, lambda request, timeout: io.BytesIO(b"wrong bytes"))
            self.assertFalse((directory / "1_F.jpg").exists())
            self.assertTrue((directory / "1_F.jpg.partial").exists())
            (directory / "1_F.jpg").write_bytes(b"bad existing")
            with self.assertRaises(ValueError): module.download_one(entry, directory, lambda request, timeout: self.fail("Must not overwrite an existing file"))

    def test_verified_partial_is_reused_and_bad_partial_preserved(self):
        data = b"published image bytes"
        entry = module.file_entry(source_file(data=data))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "1_F.jpg.partial").write_bytes(data)
            receipt = module.download_one(entry, directory, lambda request, timeout: self.fail("Verified partial must not be downloaded again"))
            self.assertTrue(receipt["reused_verified_file"])
            (directory / "1_F.jpg").unlink()
            (directory / "1_F.jpg.partial").write_bytes(b"interrupted")
            module.download_one(entry, directory, lambda request, timeout: io.BytesIO(data))
            self.assertEqual(len(list(directory.glob("*.interrupted-*"))), 1)


if __name__ == "__main__":
    unittest.main()
