import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from linux_voice_assistant.live_settings import apply_live_settings
from linux_voice_assistant.models import AvailableWakeWord, Preferences, WakeWordType


class _Player:
    def __init__(self) -> None:
        self.volumes = []

    def set_volume(self, volume: int) -> None:
        self.volumes.append(volume)


class _Peripheral:
    def __init__(self) -> None:
        self.events = []

    def emit_event_sync(self, event, data) -> None:
        self.events.append((event, data))


class _WakeInfo(AvailableWakeWord):
    def load(self):
        return SimpleNamespace(id=self.id, wake_word=self.wake_word)


class _State:
    def __init__(self, root: Path) -> None:
        wake = _WakeInfo(
            id="hey_tater",
            type=WakeWordType.MICRO_WAKE_WORD,
            wake_word="Hey Tater",
            trained_languages=["en"],
            wake_word_path=root / "hey_tater.json",
            probability_cutoff=0.98,
        )
        self.available_wake_words = {wake.id: wake}
        self.wake_words = {}
        self.active_wake_words = set()
        self.preferences = Preferences()
        self.download_dir = root
        self.wakeup_sound = "old.flac"
        self.wake_words_changed = False
        self.wake_word_1_threshold = 0.7
        self.volume = 1.0
        self.music_player = _Player()
        self.tts_player = _Player()
        self.native_settings = {}
        self.peripheral_api = _Peripheral()
        self.saved = 0

    def save_preferences(self) -> None:
        self.saved += 1


class LiveSettingsTests(unittest.TestCase):
    def test_applies_wake_sound_volume_and_led_settings_as_one_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            state = _State(Path(temporary))
            applied = apply_live_settings(
                state,
                {
                    "wake_engine": "micro_wake_word",
                    "wake_word": "hey_tater",
                    "wake_threshold": 0.92,
                    "wake_sound_enabled": True,
                    "wake_sound": "notification-ding",
                    "volume_percent": 63,
                    "led_brightness": 37,
                    "led_color": "#12abEF",
                    "led_listening_animation": "comet",
                    "led_thinking_animation": "shimmer",
                    "led_tool_call_animation": "scanner",
                    "led_replying_animation": "equalizer",
                },
            )

        self.assertEqual(state.active_wake_words, {"hey_tater"})
        self.assertEqual(state.preferences.active_wake_words, ["hey_tater"])
        self.assertEqual(state.wake_word_1_threshold, 0.92)
        self.assertTrue(state.wakeup_sound.endswith("linux_voice_assistant/assets/tater_native/notification-ding.wav"))
        self.assertTrue(Path(state.wakeup_sound).is_file())
        self.assertEqual(state.music_player.volumes, [63])
        self.assertEqual(state.tts_player.volumes, [63])
        self.assertEqual(applied["led_color"], "#12abef")
        self.assertEqual(applied["led_brightness"], 37)
        self.assertEqual(state.preferences.native_settings, applied)
        self.assertEqual(state.peripheral_api.events[0][0].value, "settings")
        self.assertEqual(state.peripheral_api.events[0][1], {"settings": applied})
        self.assertEqual(state.saved, 1)

    def test_disabling_local_wake_and_sound_is_persistent(self) -> None:
        with TemporaryDirectory() as temporary:
            state = _State(Path(temporary))
            apply_live_settings(
                state,
                {
                    "wake_engine": "button",
                    "wake_sound_enabled": False,
                    "wake_sound": "no_sound",
                },
                notify=False,
            )

        self.assertEqual(state.active_wake_words, set())
        self.assertEqual(state.preferences.active_wake_words, [])
        self.assertEqual(state.wakeup_sound, "")
        self.assertTrue(state.wake_words_changed)

    def test_invalid_led_values_fall_back_to_native_firmware_defaults(self) -> None:
        with TemporaryDirectory() as temporary:
            state = _State(Path(temporary))
            applied = apply_live_settings(
                state,
                {
                    "wake_engine": "button",
                    "led_brightness": 150,
                    "led_color": "not-a-color",
                    "led_listening_animation": "unknown",
                },
                notify=False,
            )

        self.assertEqual(applied["led_brightness"], 100)
        self.assertEqual(applied["led_color"], "#ff5a1f")
        self.assertEqual(applied["led_listening_animation"], "directional")


class SettingsAcknowledgementTests(unittest.TestCase):
    def test_native_transport_acknowledges_applied_settings(self) -> None:
        from linux_voice_assistant.tater_native import TaterNativeClient

        satellite = SimpleNamespace(
            state=SimpleNamespace(output_only=False, name="sat1", friendly_name="SAT1", version="test"),
            send_messages=lambda _messages: None,
            set_tts_segment_grace_seconds=lambda _seconds: None,
        )
        client = TaterNativeClient(satellite, url="http://tater.local:8501")
        submitted = []
        client._submit_frame = submitted.append

        with patch("linux_voice_assistant.live_settings.apply_live_settings", return_value={"wake_word": "hey_tater"}):
            client._handle_message(
                {
                    "id": "settings-1",
                    "type": "settings",
                    "payload": {"wake_word": "hey_tater"},
                }
            )

        message = json.loads(submitted[0])
        self.assertEqual(message["id"], "settings-1")
        self.assertEqual(message["type"], "settings.changed")
        self.assertTrue(message["payload"]["ok"])
        self.assertEqual(message["payload"]["settings"]["wake_word"], "hey_tater")

    def test_native_transport_reports_a_settings_error(self) -> None:
        from linux_voice_assistant.tater_native import TaterNativeClient

        state = SimpleNamespace(
            output_only=False,
            name="sat1",
            friendly_name="SAT1",
            version="test",
            native_settings={"wake_word": "previous"},
        )
        satellite = SimpleNamespace(
            state=state,
            send_messages=lambda _messages: None,
            set_tts_segment_grace_seconds=lambda _seconds: None,
        )
        client = TaterNativeClient(satellite, url="http://tater.local:8501")
        submitted = []
        client._submit_frame = submitted.append

        with self.assertLogs("linux_voice_assistant.tater_native", level="ERROR"):
            with patch("linux_voice_assistant.live_settings.apply_live_settings", side_effect=ValueError("bad settings")):
                client._handle_message({"id": "settings-2", "type": "settings", "payload": {}})

        message = json.loads(submitted[0])
        self.assertEqual(message["id"], "settings-2")
        self.assertEqual(message["type"], "settings.changed")
        self.assertFalse(message["payload"]["ok"])
        self.assertEqual(message["payload"]["error"], "bad settings")
        self.assertEqual(message["payload"]["settings"], {"wake_word": "previous"})


if __name__ == "__main__":
    unittest.main()
