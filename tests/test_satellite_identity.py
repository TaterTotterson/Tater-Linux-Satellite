import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_voice_assistant.satellite_identity import identity_text, save_identity


class SatelliteIdentityTests(unittest.TestCase):
    def test_persists_name_room_and_preserves_unknown_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tater.json"
            path.write_text(json.dumps({"tater_url": "http://ha.test", "token": "secret", "future": True}))
            save_identity(path, "Kitchen Écho", "")
            saved = json.loads(path.read_text())
            self.assertEqual(saved, {"tater_url": "http://ha.test", "token": "secret", "future": True, "name": "Kitchen Écho", "room": ""})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_atomic_replace_preserves_previous_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tater.json"
            original = '{"name":"Office","room":"Office","token":"secret"}'
            path.write_text(original)
            with patch("linux_voice_assistant.satellite_identity.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    save_identity(path, "Kitchen", "Kitchen")
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_wire_validation(self):
        self.assertEqual(identity_text(" Écho ", required=True), "Écho")
        self.assertEqual(identity_text("", required=False), "")
        self.assertEqual(identity_text("x" * 63, required=True), "x" * 63)
        for bad in (None, 42, "", "   ", "bad\nname", "bad\x00name", "é" * 32):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                identity_text(bad, required=True)
