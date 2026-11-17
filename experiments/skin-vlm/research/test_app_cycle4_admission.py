"""CPU checks for bounded, fail-closed cooperative photo admission."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from research.app_cycle4_admission import PhotoAdmission, PhotoAdmissionTimeout


class Response(io.BytesIO):
    def __init__(self, body=b'{"ready":true,"busy":false}', status=200):
        super().__init__(body)
        self.status = status


def healthy_response(*args, **kwargs):
    return Response()


class AdmissionTests(unittest.TestCase):
    def test_start_synchronously_probes_then_requires_second_healthy_poll(self):
        second = threading.Event()
        release = threading.Event()
        calls = []

        def opener(url, timeout):
            calls.append((url, timeout))
            if len(calls) == 2:
                second.set()
                release.wait(0.5)
            return Response()

        admission = PhotoAdmission(opener=opener, poll_interval=0.01)
        try:
            self.assertIs(admission.start(), admission)
            self.assertTrue(admission.busy_event.is_set())
            self.assertTrue(second.wait(0.5))
            self.assertTrue(admission.busy_event.is_set())
            release.set()
            admission.wait_idle(max_wait=1)
            self.assertFalse(admission.busy_event.is_set())
            self.assertGreaterEqual(len(calls), 2)
            self.assertTrue(all(url == PhotoAdmission.URL and timeout == 2 for url, timeout in calls))
        finally:
            release.set()
            admission.close()
        self.assertFalse(admission._thread.is_alive())
        self.assertTrue(admission.busy_event.is_set())

    def test_bad_http_body_types_missing_busy_and_exceptions_fail_closed(self):
        failures = [
            lambda: Response(status=503),
            lambda: Response(b"not-json private-photo-marker"),
            lambda: Response(b"[]"),
            lambda: Response(b'{"ready":true}'),
            lambda: Response(b'{"ready":1,"busy":false}'),
            lambda: Response(b'{"ready":true,"busy":0}'),
            lambda: Response(b'{"ready":false,"busy":false}'),
            lambda: Response(b'{"ready":true,"busy":true}'),
            lambda: Response(b'{"ready":true,"busy":false,"failed":true}'),
            lambda: Response(b"x" * (PhotoAdmission.MAX_BODY_BYTES + 1)),
            TimeoutError("private-photo-marker"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                opener = Mock(side_effect=[Response(), Response()])
                admission = PhotoAdmission(opener=opener)
                admission._poll_once()
                admission._poll_once()
                self.assertFalse(admission.busy_event.is_set())
                opener.side_effect = failure if isinstance(failure, Exception) else lambda *a, **kw: failure()
                admission._poll_once()
                self.assertTrue(admission.busy_event.is_set())
                opener.side_effect = None
                opener.return_value = Response()
                admission._poll_once()
                self.assertTrue(admission.busy_event.is_set(), "one recovery poll must not clear busy")
                opener.return_value = Response()
                admission._poll_once()
                self.assertFalse(admission.busy_event.is_set())
                admission.close()

    def test_body_read_is_bounded(self):
        response = Response(b"x" * 10000)
        response.read = Mock(wraps=response.read)
        admission = PhotoAdmission(opener=lambda *a, **kw: response)
        admission._poll_once()
        response.read.assert_called_once_with(PhotoAdmission.MAX_BODY_BYTES + 1)
        self.assertTrue(admission.busy_event.is_set())

    def test_timeout_is_bounded_and_callback_errors_propagate(self):
        admission = PhotoAdmission(opener=healthy_response)
        callback = Mock()
        started = time.monotonic()
        with self.assertRaises(PhotoAdmissionTimeout):
            admission.wait_idle(check_callback=callback, max_wait=0.04)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertGreaterEqual(callback.call_count, 2)
        callback.side_effect = RuntimeError("guard pause")
        with self.assertRaisesRegex(RuntimeError, "guard pause"):
            admission.wait_idle(check_callback=callback)
        admission.close()
        with self.assertRaises(PhotoAdmissionTimeout):
            admission.wait_idle(max_wait=1)

    def test_grace_rechecks_busy_and_stable_idle_has_no_sleep(self):
        admission = PhotoAdmission(opener=healthy_response)
        admission._poll_once()
        admission._poll_once()
        timer = threading.Timer(0.025, lambda: admission._record_poll(False))
        timer.start()
        try:
            with self.assertRaises(PhotoAdmissionTimeout):
                admission.wait_idle(max_wait=0.19)
        finally:
            timer.join()
        admission._poll_once()
        admission._poll_once()
        started = time.monotonic()
        admission.wait_idle(max_wait=1)
        self.assertGreaterEqual(time.monotonic() - started, 0.14)
        callback = Mock()
        admission._stop.wait = Mock(side_effect=AssertionError("stable idle must not sleep"))
        admission.wait_idle(check_callback=callback, max_wait=1)
        callback.assert_called_once_with()
        admission._stop.wait.assert_not_called()
        admission.close()

    def test_logs_only_transitions_and_total_wait_not_http_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "admission.jsonl"
            admission = PhotoAdmission(path, opener=healthy_response)
            admission._poll_once()
            admission._poll_once()
            admission._poll_once()
            admission.wait_idle(max_wait=1)
            admission._opener = Mock(side_effect=TimeoutError("private-photo-marker"))
            admission._poll_once()
            admission.close()
            admission.close()
            text = path.read_text()
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual([row["state"] for row in rows], ["busy", "idle", "busy", "closed"])
            self.assertTrue(all(set(row) == {"timestamp", "state", "total_wait_seconds"} for row in rows))
            self.assertTrue(all(isinstance(row["timestamp"], float) for row in rows))
            self.assertGreaterEqual(rows[-1]["total_wait_seconds"], 0.14)
            self.assertNotIn("private-photo-marker", text)
            self.assertNotIn("http", text)

    def test_stopping_criterion_uses_only_event_and_latches_per_generation(self):
        import torch
        admission = PhotoAdmission(opener=Mock(side_effect=AssertionError("token callback must not use HTTP")))
        admission.busy_event.clear()
        criterion = admission.make_stopping_criterion()
        inputs = torch.ones((3, 2), dtype=torch.long)
        first = criterion(inputs, None)
        self.assertEqual(first.shape, (3,))
        self.assertEqual(first.dtype, torch.bool)
        self.assertEqual(first.device, inputs.device)
        self.assertFalse(first.any().item())
        admission.busy_event.set()
        self.assertTrue(criterion(inputs, None).all().item())
        self.assertTrue(criterion.triggered)
        admission.busy_event.clear()
        self.assertTrue(criterion(inputs, None).all().item())
        self.assertFalse(admission.make_stopping_criterion().triggered)
        admission._opener.assert_not_called()

    def test_background_log_failure_remains_busy_and_unblocks_close(self):
        admission = PhotoAdmission(opener=healthy_response, poll_interval=0.01)
        admission._log_locked = Mock(side_effect=[None, OSError("log unavailable")])
        admission.start()
        self.assertTrue(admission._stop.wait(0.5))
        self.assertTrue(admission.busy_event.is_set())
        with self.assertRaises(PhotoAdmissionTimeout):
            admission.wait_idle(max_wait=1)
        admission._log_locked = Mock()
        admission.close()


if __name__ == "__main__":
    unittest.main()
