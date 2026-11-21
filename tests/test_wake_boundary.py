import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from aioesphomeapi.api_pb2 import VoiceAssistantRequest
from linux_voice_assistant.wake_boundary import WakeAudioBuffer, WakeBoundaryModel, select_boundary
from test_tater_native import _FakeSatellite
from linux_voice_assistant.tater_native import TaterNativeClient


class BoundaryPolicyTests(unittest.TestCase):
    def test_leading_crossing_of_strongest_run_keeps_consonant_guard(self):
        scores = [(8000, 0.83), (8320, 0.84), (8640, 0.2), (8960, 0.85), (9280, 1.0), (9600, 0.99)]
        self.assertEqual(select_boundary(scores, 12000, {}), (8960 - 1280, 1.0))

    def test_scores_outside_search_and_silence_do_not_produce_a_cut(self):
        self.assertIsNone(select_boundary([(100, 1.0), (20000, 1.0), (12000, 0.1)], 12000, {}))

    def test_capture_preserves_speech_during_verifier_and_chime(self):
        audio = WakeAudioBuffer()
        audio.feed(b"\x01\x00" * 32000)
        audio.trigger()
        audio.feed(b"\x02\x00" * 8000)
        replay, detected = audio.snapshot()
        self.assertEqual(detected, 24000)
        self.assertEqual(len(replay), (24000 + 1920) * 2)
        self.assertEqual(audio.take(), b"\x01\x00" * 24000 + b"\x02\x00" * 8000)
        self.assertFalse(audio.active)

    def test_capture_limit_is_bounded_and_explicit(self):
        audio = WakeAudioBuffer()
        audio.feed(bytes(48000))
        audio.trigger()
        audio.feed(bytes(500000))
        self.assertTrue(audio.overflow)
        self.assertLessEqual(len(audio.take()), 16000 * 12 * 2)

    def test_missing_model_fails_open_without_a_cut(self):
        model = WakeBoundaryModel()
        self.assertIsNone(model.estimate(bytes(48000), 24000, Path("/missing/model.json")))
        self.assertFalse(model.last_status["ready"])

    def test_bundled_boundary_runs_on_real_tflite_runtime(self):
        model = WakeBoundaryModel()
        result = model.estimate(bytes(48000), 24000, Path(__file__).parents[1] / "linux_voice_assistant/assets/wake/hey_tater.wake-bundle.json")
        self.assertIsNone(result)
        self.assertTrue(model.last_status["ready"], model.last_status)
        model.close()


class BoundaryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.satellite = _FakeSatellite()
        self.client = TaterNativeClient(self.satellite, url="http://test")
        self.client._connected = True
        self.client._loop = asyncio.get_running_loop()
        self.frames = []
        self.client._queue_frame = lambda frame: self.frames.append(frame) or True

    async def asyncTearDown(self):
        await self.client.close()

    async def test_marker_is_ordered_between_buffered_wake_and_full_command(self):
        self.client._wake_capture.feed(b"\x01\x00" * 24000)
        self.client._wake_capture.trigger()
        self.client._wake_capture.feed(b"\x02\x00" * 8000)
        self.client._boundary_model.estimate = lambda *_args: (22000, 0.99)
        self.client._queue_protocol_message(VoiceAssistantRequest(start=True, wake_word_phrase="hey tater"))
        await self.client._boundary_task
        self.assertEqual(len(self.frames), 1)  # no audio before voice.start.ack
        self.assertEqual(json.loads(self.frames[0])["payload"]["wake_preroll_boundary"], 1)
        self.client._handle_message({"type": "voice.start.ack", "payload": {"ok": True}})
        marker = next(i for i, value in enumerate(self.frames[1:], 1) if isinstance(value, str))
        self.assertEqual(json.loads(self.frames[marker])["type"], "voice.audio_boundary")
        self.assertEqual(b"".join(self.frames[1:marker]), b"\x01\x00" * 22000)
        self.assertEqual(b"".join(self.frames[marker + 1 :]), b"\x01\x00" * 2000 + b"\x02\x00" * 8000)

    async def test_barge_in_stop_does_not_discard_new_command_capture(self):
        self.client._wake_capture.feed(b"\x01\x00" * 8000)
        self.client._wake_capture.trigger()
        self.client._wake_capture.feed(b"\x02\x00" * 4000)
        self.client._boundary_model.estimate = lambda *_args: None

        def interrupt_then_start(word):
            self.client.send_messages([VoiceAssistantRequest(start=False)])
            self.client.send_messages([VoiceAssistantRequest(start=True, wake_word_phrase=word)])

        self.client._original_wakeup = interrupt_then_start
        self.client._start_local_wake("hey tater")
        await asyncio.sleep(0)
        await self.client._boundary_task
        start = next(json.loads(frame) for frame in self.frames if isinstance(frame, str) and json.loads(frame)["type"] == "voice.start")
        self.assertEqual(start["payload"]["wake_preroll_boundary"], 1)
        self.client._handle_message({"type": "voice.start.ack", "payload": {"ok": True}})
        self.assertEqual(b"".join(frame for frame in self.frames if isinstance(frame, bytes)), b"\x01\x00" * 8000 + b"\x02\x00" * 4000)

    async def test_unknown_phrase_keeps_entire_capture(self):
        self.client._wake_capture.feed(b"\x01\x00" * 8000)
        self.client._wake_capture.trigger()
        with patch.object(self.client._boundary_model, "estimate") as estimate:
            self.client._queue_protocol_message(VoiceAssistantRequest(start=True, wake_word_phrase="alfred"))
            await self.client._boundary_task
            estimate.assert_not_called()
        self.client._handle_message({"type": "voice.start.ack", "payload": {"ok": True}})
        self.assertEqual(json.loads(self.frames[1])["type"], "voice.audio_boundary")
        self.assertEqual(b"".join(self.frames[2:]), b"\x01\x00" * 8000)

    async def test_rejected_wake_releases_capture_and_cannot_replay_it_next_time(self):
        self.client._wake_capture.feed(bytes(32000))
        self.client._wake_capture.trigger()
        self.client._wake_verifier_requests[1] = True
        self.client._wake_verifier_pending_id = 1
        self.client._wake_verifier_pending_word = object()
        self.client._complete_wake_verification(1, accepted=False, available=True, reason="mismatch")
        self.assertFalse(self.client._wake_capture.active)
        self.assertEqual(self.satellite.wakeups, [])
