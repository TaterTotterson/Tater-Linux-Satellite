"""Overlapping wake-model installs must never share a temporary filename."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from linux_voice_assistant.tater_features import _write_bytes_atomic
from linux_voice_assistant.wake_boundary import cache_boundary_bundle


class WakeCacheWriteTests(unittest.TestCase):
    def test_overlapping_model_writes_both_finish_with_a_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "classifier.onnx"
            barrier = threading.Barrier(2)
            replace = os.replace

            def together(source, destination):
                barrier.wait(timeout=5)
                replace(source, destination)

            contents = [b"first model" * 1000, b"second model" * 1000]
            with patch("os.replace", side_effect=together), ThreadPoolExecutor(max_workers=2) as workers:
                tasks = [workers.submit(_write_bytes_atomic, target, data) for data in contents]
                for task in tasks:
                    task.result(timeout=10)
            self.assertIn(target.read_bytes(), contents)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_overlapping_boundary_installs_keep_a_verified_model_and_manifest(self):
        model = b"a verified boundary model"
        digest = hashlib.sha256(model).hexdigest()
        bundle = {
            "wake_word": "hey tater",
            "wake_boundary": {
                "model": "boundary.tflite",
                "model_sha256": digest,
                "model_size_bytes": len(model),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "custom.wake-bundle.json"
            barrier = threading.Barrier(2)
            replace = os.replace

            def together(source, destination):
                if Path(destination) == target:
                    barrier.wait(timeout=5)
                replace(source, destination)

            with patch("os.replace", side_effect=together), ThreadPoolExecutor(max_workers=2) as workers:
                tasks = [workers.submit(cache_boundary_bundle, bundle, "https://example.org/bundle.json", target, lambda url: model) for _ in range(2)]
                for task in tasks:
                    task.result(timeout=10)
            cached = json.loads(target.read_text())
            self.assertEqual(hashlib.sha256((target.parent / cached["wake_boundary"]["model"]).read_bytes()).hexdigest(), digest)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_failed_install_preserves_existing_model_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "classifier.onnx"
            target.write_bytes(b"working model")
            with patch("os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    _write_bytes_atomic(target, b"replacement model")
            self.assertEqual(target.read_bytes(), b"working model")
            self.assertEqual(list(Path(directory).iterdir()), [target])
