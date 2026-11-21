import asyncio
import json
import tempfile
import types
import time
import unittest
from pathlib import Path
from unittest import mock

from linux_voice_assistant import tater_features
from linux_voice_assistant.peripheral_api import LVAEvent as _Event


class _Player:
    def __init__(self) -> None:
        self.volume = 100
        self.played = []
        self.done_callback = None
        self.duck_factor = None
        self.synchronized_controls_available = True
        self.prepared = []
        self.snapshot = {
            "loaded": True,
            "position_seconds": 0.0,
            "buffered_seconds": 2.0,
            "buffered_seconds_known": True,
            "duration_seconds": 5.0,
            "seeking": False,
            "rebuffering": False,
            "paused": False,
            "speed": 1.0,
        }
        self.speed = 1.0
        self.resume_count = 0
        self.synchronized_resume_count = 0
        self.pause_count = 0
        self.stop_count = 0
        self.jumps = []

    def set_volume(self, value):
        self.volume = value

    def play(self, url, done_callback=None, stop_first=False):
        self.played.append((url, stop_first))
        self.done_callback = done_callback

    def prepare_synchronized(self, url, *, channel="stereo", loop=False, done_callback=None):
        self.prepared.append((url, channel, loop))
        self.done_callback = done_callback
        self.snapshot["paused"] = True

    def synchronized_snapshot(self):
        return dict(self.snapshot)

    def seek_synchronized(self, position_seconds):
        self.snapshot["position_seconds"] = position_seconds

    def jump_synchronized(self, delta_seconds):
        self.jumps.append(delta_seconds)
        self.snapshot["position_seconds"] += delta_seconds

    def set_synchronized_speed(self, speed):
        self.speed = speed
        self.snapshot["speed"] = speed

    def reset_synchronized(self):
        self.speed = 1.0
        self.snapshot["speed"] = 1.0

    def resume(self):
        self.resume_count += 1
        self.snapshot["paused"] = False

    def resume_synchronized(self):
        self.synchronized_resume_count += 1
        raise RuntimeError("S420 python-mpv async resume is unavailable")

    def pause(self):
        self.pause_count += 1
        self.snapshot["paused"] = True

    def stop(self):
        self.stop_count += 1
        callback = self.done_callback
        self.done_callback = None
        if callback is not None:
            callback()

    def duck(self, factor=0.5):
        self.duck_factor = factor

    def unduck(self):
        self.duck_factor = None


class _Preferences:
    def __init__(self) -> None:
        self.active_wake_words = ["hey_tater"]
        self.wake_word_1_sensitivity = None


class _AvailableWakeWord:
    def __init__(self, wake_word="Hey Tater") -> None:
        self.wake_word = wake_word
        self.loaded = object()
        self.load_count = 0

    def load(self):
        self.load_count += 1
        return self.loaded


class _State:
    def __init__(self) -> None:
        self.music_player = _Player()
        self.tts_player = _Player()
        self.stop_word = types.SimpleNamespace(id="stop")
        self.active_wake_words = {"hey_tater"}
        self.available_wake_words = {"hey_tater": _AvailableWakeWord()}
        self.wake_words = {}
        self.download_dir = Path(tempfile.gettempdir())
        self.preferences = _Preferences()
        self.wake_word_1_threshold = 0.97
        self.wake_words_changed = False
        self.muted = False
        self.volume = 0.8
        self.saved = 0
        self.wakeup_sound = "/factory/wake_word_triggered.flac"

    def persist_volume(self, value):
        self.volume = value

    def save_preferences(self):
        self.saved += 1


class _Satellite:
    def __init__(self, state) -> None:
        self.state = state
        self._timer_finished = False
        self._timer_ring_start = None
        self.events = []
        self.muted = False
        self.verified_wakes = []

    def _emit(self, event, data=None):
        self.events.append((event, data))

    def _play_timer_finished(self):
        pass

    def duck(self):
        self.state.music_player.duck()

    def unduck(self):
        self.state.music_player.unduck()

    def _set_muted(self, value):
        self.muted = value
        self.state.muted = value

    def _tater_start_verified_wake(self, wake_word_phrase):
        self.verified_wakes.append(wake_word_phrase)


class _Client:
    def __init__(self) -> None:
        self.state = _State()
        self.satellite = _Satellite(self.state)
        self.frames = []
        self.binary_frames = []
        self.connected = True
        self._loop = None

    def _submit_frame(self, frame):
        if isinstance(frame, (bytes, bytearray)):
            self.binary_frames.append(bytes(frame))
        else:
            self.frames.append(json.loads(frame))


class SharedFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = _Client()
        self.client.uses_shared_features = True
        self.client._loop = asyncio.get_running_loop()
        self.manager = tater_features.TaterFeatureManager(self.client)

    async def asyncTearDown(self):
        await self.manager.close()

    def _messages(self, kind):
        return [row for row in self.client.frames if row["type"] == kind]

    async def test_fresh_boot_uses_model_calibration_and_preserves_explicit_tuning(self):
        self.client.state.wake_word_1_threshold = 0.7
        self.client.state.available_wake_words["hey_tater"].probability_cutoff = 0.98
        manager = tater_features.TaterFeatureManager(self.client)
        self.assertEqual(manager._configured_wake_threshold, 0.98)
        await manager.close()
        self.client.state.preferences.wake_word_1_sensitivity = 0.93
        manager = tater_features.TaterFeatureManager(self.client)
        self.assertEqual(manager._configured_wake_threshold, 0.93)
        await manager.close()

    async def test_dual_mode_requires_both_detector_events(self) -> None:
        class Worker:
            def __init__(self) -> None:
                self.event = None

            def pop_detection(self):
                event, self.event = self.event, None
                return event

            def close(self):
                pass

            def status(self):
                return {"ready": True}

        worker = Worker()
        self.manager.settings["wake_detector_mode"] = "dual"
        self.manager._oww_worker = worker  # pylint: disable=protected-access
        worker.event = (time.monotonic(), 0.91)
        self.assertFalse(self.manager.resolve_wake_detection(False))
        self.assertTrue(self.manager.resolve_wake_detection(True))
        self.assertEqual(self.manager._oww_agreements, 1)  # pylint: disable=protected-access

    async def test_oww_only_mode_does_not_require_mww(self) -> None:
        worker = mock.Mock()
        worker.pop_detection.return_value = (time.monotonic(), 0.75)
        worker.status.return_value = {"ready": True}
        self.manager.settings["wake_detector_mode"] = "oww"
        self.manager._oww_worker = worker  # pylint: disable=protected-access
        self.assertTrue(self.manager.resolve_wake_detection(False))
        worker.close.assert_not_called()

    async def test_nww_only_mode_does_not_require_mww(self) -> None:
        worker = mock.Mock()
        worker.pop_detection.return_value = (time.monotonic(), 0.995)
        worker.status.return_value = {"ready": True}
        self.manager.settings["wake_detector_mode"] = "nww"
        self.manager._oww_worker = worker  # pylint: disable=protected-access
        self.assertFalse(self.manager.wake_mww_enabled())
        self.assertTrue(self.manager.resolve_wake_detection(False))

    async def test_bundle_selects_cnn_and_gru_nanowakeword_variants(self) -> None:
        bundle = {
            "type": "tater_wake_word_bundle",
            "wake_word": "Hey Tater",
            "nano_wake_word": {
                "default_architecture": "cnn",
                "variants": {
                    "cnn": {
                        "model": "hey_tater.nww.cnn.onnx",
                        "model_sha256": "a" * 64,
                        "model_size_bytes": 123,
                        "recommended_threshold": 0.99,
                        "recommended_patience": 3,
                    },
                    "gru": {
                        "model": "hey_tater.nww.gru.onnx",
                        "model_sha256": "b" * 64,
                        "model_size_bytes": 456,
                        "recommended_threshold": 0.98,
                        "recommended_patience": 4,
                    },
                },
            },
        }
        phrase, _variant, artifact, threshold, patience = tater_features._nww_bundle_details(  # pylint: disable=protected-access
            bundle,
            architecture="gru",
        )
        self.assertEqual(phrase, "Hey Tater")
        self.assertEqual(artifact["file"], "hey_tater.nww.gru.onnx")
        self.assertEqual((threshold, patience), (0.98, 4))
        with self.assertRaisesRegex(ValueError, "unsupported NanoWakeWord"):
            tater_features._nww_bundle_details(bundle, architecture="dnn")  # pylint: disable=protected-access

    async def test_bundle_uses_mode_specific_calibration(self) -> None:
        bundle = {
            "type": "tater_wake_word_bundle",
            "wake_word": "Hey Tater",
            "open_wake_word": {
                "metadata": "hey_tater.oww.json",
                "metadata_sha256": "a" * 64,
                "artifacts": {
                    "onnx": {
                        "file": "hey_tater.oww.onnx",
                        "sha256": "b" * 64,
                        "size_bytes": 123,
                    }
                },
                "recommended_threshold": 0.56,
                "recommended_patience": 3,
                "recommended_confirmation_threshold": 0.90,
                "recommended_confirmation_patience": 2,
            },
        }
        _phrase, _section, threshold, patience = tater_features._oww_bundle_details(  # pylint: disable=protected-access
            bundle,
            confirmation=False,
        )
        self.assertEqual((threshold, patience), (0.56, 3))
        _phrase, _section, threshold, patience = tater_features._oww_bundle_details(  # pylint: disable=protected-access
            bundle,
            confirmation=True,
        )
        self.assertEqual((threshold, patience), (0.90, 2))
        self.assertNotIn("audio_session_version", self.manager.capabilities)

    async def test_native_overlay_remains_available_with_sendspin(self) -> None:
        self.manager.handle_message(
            {
                "type": "audio.overlay.start",
                "payload": {
                    "overlay_id": "reply-1",
                    "foreground": {"url": "https://tater.test/reply.flac"},
                    "ducking": {
                        "target_percent": 25,
                        "attack_ms": 0,
                        "release_ms": 0,
                    },
                },
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.client.state.music_player.duck_factor, 0.25)
        self.assertTrue(self._messages("audio.overlay.started"))
        self.assertIn(
            (_Event.LIGHT_COMMAND, {"state": True, "effect": "speaking"}),
            self.client.satellite.events,
        )
        self.client.state.tts_player.done_callback()
        await asyncio.sleep(0.01)
        self.assertIsNone(self.client.state.music_player.duck_factor)
        self.assertTrue(self._messages("audio.overlay.finished"))
        self.assertIn(
            (_Event.LIGHT_COMMAND, {"state": False}),
            self.client.satellite.events,
        )

    async def test_synchronized_overlay_honors_audible_deadline(self) -> None:
        start_at_us = (tater_features.time.monotonic_ns() // 1000) + 180_000
        self.manager.handle_message(
            {
                "type": "audio.overlay.start",
                "payload": {
                    "overlay_id": "scheduled-reply",
                    "group_id": "kitchen-pair",
                    "start_at_us": start_at_us,
                    "foreground": {"url": "https://tater.test/reply.flac"},
                    "ducking": {"target_percent": 30, "attack_ms": 0, "release_ms": 0},
                },
            }
        )
        await asyncio.sleep(0.02)
        self.assertFalse(self._messages("audio.overlay.started"))
        await asyncio.sleep(0.18)
        started = self._messages("audio.overlay.started")[-1]["payload"]
        self.assertEqual(started["scheduled_start_us"], start_at_us)
        self.assertEqual(started["group_id"], "kitchen-pair")
        self.assertGreaterEqual(self.client.state.tts_player.resume_count, 1)
        self.client.state.tts_player.done_callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_audio_scene_mixes_background_and_foreground_then_fades(self) -> None:
        self.manager.handle_message(
            {
                "type": "audio.scene.start",
                "payload": {
                    "scene_id": "weather-scene",
                    "foreground": {
                        "url": "https://tater.test/weather.flac",
                        "volume_percent": 95,
                    },
                    "background": {
                        "url": "https://tater.test/bed.flac",
                        "volume_percent": 60,
                        "loop": True,
                    },
                    "ducking": {"target_percent": 35, "attack_ms": 0, "release_ms": 0},
                    "finish": {"fade_ms": 0},
                },
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(
            self.client.state.music_player.prepared[-1],
            ("https://tater.test/bed.flac", "stereo", True),
        )
        self.assertEqual(
            self.client.state.tts_player.prepared[-1],
            ("https://tater.test/weather.flac", "mono", False),
        )
        self.assertEqual(self.client.state.music_player.duck_factor, 0.35)
        self.assertGreaterEqual(self.client.state.music_player.resume_count, 1)
        self.assertGreaterEqual(self.client.state.tts_player.resume_count, 1)
        self.assertIn(
            (_Event.LIGHT_COMMAND, {"state": True, "effect": "speaking"}),
            self.client.satellite.events,
        )
        self.client.state.tts_player.done_callback()
        await asyncio.sleep(0.01)
        finished = self._messages("audio.scene.finished")[-1]["payload"]
        self.assertEqual(finished, {"scene_id": "weather-scene", "ok": True})
        self.assertIsNone(self.client.state.music_player.duck_factor)
        self.assertIn(
            (_Event.LIGHT_COMMAND, {"state": False}),
            self.client.satellite.events,
        )

    async def test_overlay_watchdog_resets_stalled_tts_and_restores_music(self) -> None:
        original_watchdog_interval = tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS
        original_stall_timeout = tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS
        tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS = 0.01
        tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS = 0.03
        try:
            self.manager.handle_message(
                {
                    "type": "audio.overlay.start",
                    "payload": {
                        "overlay_id": "stalled-overlay",
                        "foreground": {"url": "https://tater.test/stalled-tts.flac"},
                        "ducking": {
                            "target_percent": 20,
                            "attack_ms": 0,
                            "release_ms": 0,
                        },
                    },
                }
            )
            await asyncio.sleep(0.1)

            self.assertIsNone(self.manager.overlay_session)
            self.assertGreaterEqual(self.client.state.tts_player.stop_count, 1)
            self.assertIsNone(self.client.state.music_player.duck_factor)
            finished = self._messages("audio.overlay.finished")[-1]["payload"]
            self.assertEqual(finished["overlay_id"], "stalled-overlay")
            self.assertFalse(finished["ok"])
        finally:
            tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS = original_watchdog_interval
            tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS = original_stall_timeout

    async def test_overlay_watchdog_allows_normal_render_progress(self) -> None:
        original_watchdog_interval = tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS
        original_stall_timeout = tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS
        tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS = 0.01
        tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS = 0.04
        try:
            self.manager.handle_message(
                {
                    "type": "audio.overlay.start",
                    "payload": {
                        "overlay_id": "healthy-overlay",
                        "foreground": {"url": "https://tater.test/healthy-tts.flac"},
                        "ducking": {
                            "target_percent": 20,
                            "attack_ms": 0,
                            "release_ms": 0,
                        },
                    },
                }
            )

            async def advance_and_finish() -> None:
                for position in (0.01, 0.02, 0.03, 0.04):
                    await asyncio.sleep(0.01)
                    self.client.state.tts_player.snapshot["position_seconds"] = position
                callback = self.client.state.tts_player.done_callback
                self.assertIsNotNone(callback)
                callback()

            await advance_and_finish()
            await asyncio.sleep(0.02)

            self.assertIsNone(self.manager.overlay_session)
            finished = self._messages("audio.overlay.finished")[-1]["payload"]
            self.assertEqual(finished["overlay_id"], "healthy-overlay")
            self.assertTrue(finished["ok"])
            self.assertEqual(self.client.state.tts_player.stop_count, 0)
        finally:
            tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS = original_watchdog_interval
            tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS = original_stall_timeout

    async def test_audio_scene_watchdog_stops_stalled_players(self) -> None:
        original_watchdog_interval = tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS
        original_stall_timeout = tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS
        tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS = 0.01
        tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS = 0.03
        try:
            self.manager.handle_message(
                {
                    "type": "audio.scene.start",
                    "payload": {
                        "scene_id": "stalled-scene",
                        "foreground": {"url": "https://tater.test/stalled-scene.flac"},
                        "background": {
                            "url": "https://tater.test/stalled-bed.flac",
                            "loop": True,
                        },
                        "ducking": {"attack_ms": 0, "release_ms": 0},
                        "finish": {"fade_ms": 0},
                    },
                }
            )
            await asyncio.sleep(0.1)

            self.assertIsNone(self.manager.audio_scene)
            self.assertGreaterEqual(self.client.state.tts_player.stop_count, 1)
            self.assertGreaterEqual(self.client.state.music_player.stop_count, 1)
            self.assertIsNone(self.client.state.music_player.duck_factor)
            finished = self._messages("audio.scene.finished")[-1]["payload"]
            self.assertEqual(finished["scene_id"], "stalled-scene")
            self.assertFalse(finished["ok"])
        finally:
            tater_features._PLAYBACK_WATCHDOG_INTERVAL_SECONDS = original_watchdog_interval
            tater_features._PLAYBACK_STALL_TIMEOUT_SECONDS = original_stall_timeout
