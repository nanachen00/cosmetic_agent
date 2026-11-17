"""No SSH/GPU: fake checkpoint transfers and adversarial path/hash checks."""
import hashlib
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research import watch_app_cycle4 as watch


class FakeRemote:
    def __init__(self):
        self.report, self.checkpoints = watch.run_paths(watch.DEFAULT_RUN)
        self.payload = {"adapter_model.safetensors": b"adapter", "state.pt": b"optimizer and tensor RNG",
                        "state.json": b"{}", "metadata.json": b"{}", "python-random-state.json": b"[]"}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in self.payload.items()}
        self.payload[watch.MANIFEST] = json.dumps({"schema_version": 1, "files": hashes}).encode()
        self.hashes = {name: hashlib.sha256(value).hexdigest() for name, value in self.payload.items()}
        self.copies = []
        self.fail_member = None
        self.corrupt_member = None
        self.status = "baseline_validation"
        self.alive = False
        self.artifacts = {}

    def state(self):
        return {"job": {"pid": 123}, "pid_alive": self.alive, "status": {"status": self.status},
                "completed_trials": 0, "targets": [{"kind": "pilot", "checkpoint": str(self.checkpoints / "pilot/step-000001")}],
                "artifacts": self.artifacts}

    def describe(self, checkpoint):
        return {"files": self.hashes, "manifest_sha256": self.hashes[watch.MANIFEST], "symlinks": False,
                "sizes": {name: len(value) for name, value in self.payload.items()}}

    def download(self, checkpoint, member, target):
        self.copies.append(member)
        if member == self.fail_member:
            target.write_bytes(self.payload[member][:5])
            raise watch.ConnectionUnavailable("fake network interruption")
        if member == self.corrupt_member:
            target.write_bytes(b"bad bytes")
        elif target.exists() and target.stat().st_size:
            old = target.read_bytes()
            target.write_bytes(old + self.payload[member][len(old):])
        else:
            target.write_bytes(self.payload[member])


class CheckpointWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name).resolve() / "mac"
        self.remote = FakeRemote()
        self.watcher = watch.Watcher(self.remote, self.local)

    def destination(self):
        return self.local / "mac-checkpoints" / self.remote.report.name / "pilot/step-000001"

    def test_full_backup_includes_optimizer_rng_and_original_manifest(self):
        self.assertFalse(self.watcher.poll())
        destination = self.destination()
        self.assertEqual({p.name for p in destination.iterdir()}, set(self.remote.payload))
        for name, value in self.remote.payload.items():
            self.assertEqual((destination / name).read_bytes(), value)
        self.assertEqual(self.watcher.record["counts"]["complete_checkpoints"], 1)
        self.assertEqual(self.watcher.record["training_status"], "baseline_validation")
        self.assertEqual(self.watcher.record["watcher_state"], "worker_not_running_status_preserved")

    def test_completed_backup_is_not_downloaded_again(self):
        self.watcher.poll()
        first = list(self.remote.copies)
        self.watcher.poll()
        self.assertEqual(self.remote.copies, first)
        restarted = watch.Watcher(self.remote, self.local)
        restarted.poll()
        self.assertEqual(self.remote.copies, first)

    def test_interruption_resumes_only_unverified_files(self):
        self.remote.fail_member = "state.pt"
        with self.assertRaises(watch.ConnectionUnavailable):
            self.watcher.poll()
        self.assertFalse(self.destination().exists())
        self.assertEqual(self.remote.copies.count("adapter_model.safetensors"), 1)
        self.remote.fail_member = None
        restarted = watch.Watcher(self.remote, self.local)
        restarted.poll()
        self.assertEqual(self.remote.copies.count("adapter_model.safetensors"), 1)
        self.assertEqual(restarted.record["counts"]["complete_checkpoints"], 1)
        self.assertFalse(self.destination().with_name("step-000001.partial").exists())

    def test_hash_mismatch_never_publishes_success(self):
        self.remote.corrupt_member = "state.pt"
        with self.assertRaisesRegex(watch.RetryDownload, "SHA mismatch"):
            self.watcher.poll()
        self.assertFalse(self.destination().exists())
        self.assertFalse(self.watcher.record["checkpoints"]["pilot/step-000001"]["complete"])
        rejected = self.watcher.record["checkpoints"]["pilot/step-000001"]["rejected_partials"]
        self.assertEqual(Path(rejected[0]["preserved_path"]).read_bytes(), b"bad bytes")
        self.remote.corrupt_member = None
        self.watcher.poll()
        self.assertTrue(self.destination().exists())

    def test_unsafe_manifest_paths_are_rejected_before_transfer(self):
        for name in ("/absolute", "../outside", "a/../b", "a//b", "a/./b", "a\\b", "x:y", "x;rm", "x y", "x\ny"):
            with self.subTest(name=name), self.assertRaises(watch.IntegrityError):
                watch.validate_descriptor({"files": {name: "a" * 64, watch.MANIFEST: "b" * 64},
                    "manifest_sha256": "b" * 64, "symlinks": False})
        self.assertEqual(self.remote.copies, [])

    def test_remote_and_local_symlinks_are_rejected(self):
        with self.assertRaises(watch.IntegrityError):
            watch.validate_descriptor({"files": self.remote.hashes, "manifest_sha256": self.remote.hashes[watch.MANIFEST], "symlinks": True})
        partial = self.destination().with_name("step-000001.partial")
        partial.parent.mkdir(parents=True)
        other = self.local / "other"
        other.mkdir()
        partial.symlink_to(other, target_is_directory=True)
        with self.assertRaises(watch.IntegrityError):
            self.watcher.poll()
        self.assertEqual(self.remote.copies, [])

    def test_remote_inspector_rejects_symlink_without_reading_target(self):
        root = Path(self.tmp.name).resolve() / "remote"
        folder = root / "pilot/step-000001"
        folder.mkdir(parents=True)
        for name, data in self.remote.payload.items():
            (folder / name).write_bytes(data)
        (folder / "state.pt").unlink()
        outside = root / "not-a-real-secret"
        outside.write_text("must not be copied")
        (folder / "state.pt").symlink_to(outside)
        request = dict(operation="checkpoint", checkpoints=str(root), relative="pilot/step-000001")
        script = watch.REMOTE.replace("REQUEST", repr(request), 1)
        result = subprocess.run([sys.executable, "-"], input=script, text=True, capture_output=True, check=True)
        response = json.loads(result.stdout)
        self.assertFalse(response["ok"])
        self.assertFalse(response["retry"])
        self.assertNotIn("must not be copied", result.stdout)

    def test_remote_run_is_scoped_and_future_r3_is_isolated(self):
        report, checkpoints = watch.run_paths(watch.DEFAULT_RUN.replace("r2", "r3"))
        self.assertEqual(report.name, "qwen-app-cycle4-20261116-r3")
        self.assertEqual(checkpoints.name, report.name)
        for path in ("/mnt/goodagents/secrets/run", watch.DEFAULT_RUN + "/../r3", "https://host/run"):
            with self.subTest(path=path), self.assertRaises(watch.IntegrityError):
                watch.run_paths(path)

    def test_checkpoint_cannot_escape_this_remote_run(self):
        with self.assertRaises(watch.IntegrityError):
            watch.checkpoint_relative("/mnt/goodagents/other/step-000001", self.remote.checkpoints)
        with self.assertRaises(watch.IntegrityError):
            watch.checkpoint_relative(str(self.remote.checkpoints / "pilot/step-000001.partial"), self.remote.checkpoints)

    def test_scp_uses_batch_controlpath_and_no_credentials_in_args(self):
        remote = watch.Remote(watch.DEFAULT_RUN)
        child = type("Child", (), {"wait": lambda self, **kwargs: 0})()
        with patch.object(watch.subprocess, "Popen", return_value=child) as run:
            remote.download("pilot/step-000001", "state.pt", self.local / "state.pt")
        args = run.call_args.args[0]
        self.assertIn("BatchMode=yes", args)
        self.assertIn("ControlPath=" + watch.CONTROL, args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertEqual(args[args.index("-l") + 1], "4096")

    def test_explicit_bandwidth_and_invalid_limits(self):
        remote = watch.Remote(watch.DEFAULT_RUN, bandwidth_kbps=2048)
        child = type("Child", (), {"wait": lambda self, **kwargs: 0})()
        with patch.object(watch.subprocess, "Popen", return_value=child) as run:
            remote.download("pilot/step-000001", "state.pt", self.local / "state.pt")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-l") + 1], "2048")
        for value in (0, -1, True, "4096", 1000001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                watch.Remote(watch.DEFAULT_RUN, bandwidth_kbps=value)

    def test_sftp_pipeline_has_fixed_eight_requests_and_16k_buffer(self):
        remote = watch.Remote(watch.DEFAULT_RUN)
        child = type("Child", (), {"wait": lambda self, **kwargs: 0})()
        with patch.object(watch.subprocess, "Popen", return_value=child) as run:
            remote.download("pilot/step-000001", "state.pt", self.local / "state.pt")
        args = run.call_args.args[0]
        self.assertEqual([args[n + 1] for n, value in enumerate(args) if value == "-X"],
                         ["nrequests=8", "buffer=16384"])

    def test_nonempty_partial_uses_sftp_reget_with_safe_batch_and_same_auth(self):
        remote = watch.Remote(watch.DEFAULT_RUN)
        target = self.local / 'partial with "quotes" [x].downloading'
        target.write_bytes(b"retained prefix")
        child = unittest.mock.Mock()
        child.wait.return_value = 0
        with patch.object(watch.subprocess, "Popen", return_value=child) as run:
            remote.download("pilot/step-000001", "state.pt", target)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "sftp")
        for flag, value in (("-R", "8"), ("-B", "16384"), ("-l", "4096"), ("-b", "-")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("BatchMode=yes", args)
        self.assertIn("ControlPath=" + watch.CONTROL, args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        batch = child.stdin.write.call_args.args[0].decode()
        self.assertEqual(batch, '@reget ' + watch.sftp_quote(remote.checkpoints / "pilot/step-000001/state.pt") + ' ' + watch.sftp_quote(target) + '\nbye\n')
        self.assertEqual(batch.count("\n"), 2)
        self.assertEqual(target.read_bytes(), b"retained prefix")

    def test_sftp_quote_escapes_globs_quotes_and_rejects_command_newlines(self):
        self.assertEqual(watch.sftp_quote('/tmp/a "b" [x]*?\\y'), '"/tmp/a \\"b\\" \\[x\\]\\*\\?\\\\y"')
        for path in ("x\n!bad", "x\rbye", "x\x00y", "x\tbad"):
            with self.subTest(path=repr(path)), self.assertRaises(watch.IntegrityError):
                watch.sftp_quote(path)

    def test_bad_existing_prefix_is_preserved_then_fresh_retry_passes_full_sha(self):
        partial = self.destination().with_name("step-000001.partial")
        partial.mkdir(parents=True)
        (partial / "state.pt.downloading").write_bytes(b"wrong")
        with self.assertRaises(watch.RetryDownload):
            self.watcher.poll()
        self.assertFalse(self.destination().exists())
        self.watcher.poll()
        self.assertEqual((self.destination() / "state.pt").read_bytes(), self.remote.payload["state.pt"])

    def test_oversized_partial_is_quarantined_before_fresh_copy(self):
        partial = self.destination().with_name("step-000001.partial")
        partial.mkdir(parents=True)
        (partial / "state.pt.downloading").write_bytes(b"oversized" * 20)
        self.watcher.poll()
        record = self.watcher.record["checkpoints"]["pilot/step-000001"]
        self.assertTrue(record["complete"])
        self.assertEqual(record["rejected_partials"][0]["reason"], "oversized_partial")

    def test_scp_long_transfer_polls_metadata_on_heartbeat(self):
        remote = watch.Remote(watch.DEFAULT_RUN)
        child = unittest.mock.Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired("scp", 60), 0]
        remote.heartbeat = unittest.mock.Mock()
        with patch.object(watch.subprocess, "Popen", return_value=child):
            remote.download("pilot/step-000001", "state.pt", self.local / "state.pt")
        remote.heartbeat.assert_called_once_with()

    def test_reget_timeout_keeps_byte_prefix_for_next_attempt_and_heartbeats(self):
        remote = watch.Remote(watch.DEFAULT_RUN)
        target = self.local / "state.pt.downloading"
        target.write_bytes(b"existing byte prefix")
        remote.heartbeat = unittest.mock.Mock()
        child = unittest.mock.Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired("sftp", 60), 0]
        with patch.object(watch.subprocess, "Popen", return_value=child), \
                patch.object(watch.time, "monotonic", side_effect=[0, 0, 1801]):
            with self.assertRaises(watch.ConnectionUnavailable):
                remote.download("pilot/step-000001", "state.pt", target)
        remote.heartbeat.assert_called_once_with()
        child.terminate.assert_called_once_with()
        self.assertEqual(target.read_bytes(), b"existing byte prefix")
        child = unittest.mock.Mock()
        child.wait.return_value = 0
        with patch.object(watch.subprocess, "Popen", return_value=child) as run:
            remote.download("pilot/step-000001", "state.pt", target)
        self.assertEqual(run.call_args.args[0][0], "sftp")

    def test_report_artifacts_keep_original_bytes_and_sha_without_logging_contents(self):
        raw = b'{"fingerprint":"test-only-marker-not-to-log", "metrics":{"macro_f1":0.6}}\n'
        h = hashlib.sha256(raw).hexdigest()
        self.remote.artifacts = {name: {"content": raw.decode(), "sha256": h}
                                 for name in ("protocol.json", "selection.json", "progress.json")}
        self.remote.status = "complete"
        with patch.object(watch, "Remote", return_value=self.remote), \
                patch.object(sys, "argv", ["watch", "--once", "--local-root", str(self.local)]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watch.main(), 0)
        self.assertNotIn("test-only-marker-not-to-log", output.getvalue())
        status = json.loads((self.local / "watch-status.json").read_text())
        for name in self.remote.artifacts:
            self.assertEqual((self.local / "remote-reports" / name).read_bytes(), raw)
            self.assertEqual(status["remote_report_artifacts"][name]["sha256"], h)

    def test_unlisted_or_bad_hash_report_artifacts_are_rejected(self):
        for artifacts in ({"worker.log": {"content": "{}", "sha256": "a" * 64}},
                          {"protocol.json": {"content": "{}", "sha256": "a" * 64}}):
            self.remote.artifacts = artifacts
            with self.assertRaises(watch.IntegrityError):
                self.watcher.poll()
        self.assertFalse((self.local / "remote-reports").exists())

    def test_suspended_status_is_preserved_without_claiming_complete_training(self):
        self.remote.status = "suspended_for_photo_latency"
        self.assertTrue(self.watcher.poll())
        self.assertEqual(self.watcher.record["terminal_training_status"], "suspended_for_photo_latency")
        self.assertNotEqual(self.watcher.record["training_status"], "complete")

    def test_network_retry_does_not_turn_training_into_failed(self):
        with patch.object(watch, "Remote", return_value=self.remote), \
                patch.object(self.remote, "state", side_effect=watch.ConnectionUnavailable("offline")), \
                patch.object(watch.time, "sleep", side_effect=StopIteration), \
                patch.object(sys, "argv", ["watch", "--local-root", str(self.local)]):
            with self.assertRaises(StopIteration):
                watch.main()
        status = json.loads((self.local / "watch-status.json").read_text())
        self.assertEqual(status["watcher_state"], "retrying_connection")
        self.assertNotEqual(status["training_status"], "failed")


if __name__ == "__main__":
    unittest.main()
