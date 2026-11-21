import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from linux_voice_assistant.sendspin import SendspinPlayer


class SendspinTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_starts_once_and_stops_owned_child(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TATER_DATA_DIR": directory, "TATER_SENDSPIN_BINARY": "/usr/bin/true"}):
            player = SendspinPlayer("stable-device", "Office Speaker")
            player.output_channel_mode = "left"
            finished = asyncio.Event()
            child = type("Child", (), {})()
            child.returncode = None

            async def wait():
                await finished.wait()
                child.returncode = 0
                return 0

            def terminate():
                finished.set()

            child.wait = wait
            child.terminate = terminate
            child.kill = terminate
            with (
                patch("linux_voice_assistant.sendspin.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=child) as spawn,
                patch("linux_voice_assistant.sendspin.shutil.which", return_value=None),
            ):
                player.start()
                player.start()
                await asyncio.sleep(0)
                self.assertEqual(spawn.await_count, 1)
                self.assertIn("--state-dir", spawn.await_args.args)
                self.assertEqual(spawn.await_args.kwargs["env"]["TATER_SENDSPIN_OUTPUT_CHANNEL"], "left")
                self.assertTrue(player.status()["running"])
                await player.close()
                self.assertTrue(finished.is_set())
                self.assertFalse(player.status()["running"])

    async def test_ducking_only_changes_this_players_stream_and_restores_volume(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TATER_DATA_DIR": directory, "TATER_SENDSPIN_BINARY": "/usr/bin/true"}):
            player = SendspinPlayer("stable-device", "Office")
            streams = [
                {"index": 4, "properties": {"application.name": player.app_name}, "volume": {"front-left": {"value": 40000}, "front-right": {"value": 50000}}},
                {"index": 9, "properties": {"application.name": "Other music"}, "volume": {"mono": {"value": 60000}}},
            ]
            calls = []

            async def pactl(*args):
                calls.append(args)
                return json.dumps(streams).encode() if args[0] == "--format=json" else b""

            player._pactl = pactl
            with patch("linux_voice_assistant.sendspin.shutil.which", return_value="pactl"):
                player.duck(0.2)
                await player._duck_task
                player.duck(1)
                await player._duck_task
                self.assertIn(("set-sink-input-volume", "4", "8000", "10000"), calls)
                self.assertIn(("set-sink-input-volume", "4", "40000", "50000"), calls)
                self.assertFalse(any(row[0] == "set-sink-input-volume" and row[1] == "9" for row in calls))
                await player.close()

    async def test_external_service_is_not_started_or_stopped_by_control_client(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TATER_DATA_DIR": directory, "TATER_SENDSPIN_BINARY": "/usr/bin/true"}):
            player = SendspinPlayer("stable-device", "Office")
            player.external = True
            with patch("linux_voice_assistant.sendspin.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn, patch("linux_voice_assistant.sendspin.shutil.which", return_value=None):
                player.start()
                await player.close()
                spawn.assert_not_called()
