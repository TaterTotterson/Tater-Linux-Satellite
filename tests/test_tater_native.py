import asyncio
import json
import unittest

import websockets
from aioesphomeapi.api_pb2 import VoiceAssistantAnnounceFinished, VoiceAssistantAudio, VoiceAssistantRequest
from aioesphomeapi.model import VoiceAssistantEventType

from linux_voice_assistant.tater_native import TaterNativeClient, _event_data, _voice_event, normalize_tater_url


def _json(frame: str) -> dict:
    return json.loads(frame)


class TaterNativeTests(unittest.TestCase):
    def test_normalize_tater_url_accepts_base_and_websocket_urls(self) -> None:
        self.assertEqual(normalize_tater_url("tater.local:8501"), "ws://tater.local:8501/api/tater/satellite/v1/ws")
        self.assertEqual(normalize_tater_url("https://tater.example"), "wss://tater.example/api/tater/satellite/v1/ws")
        self.assertEqual(normalize_tater_url("ws://tater.local:8501/api/tater/satellite/v1/ws"), "ws://tater.local:8501/api/tater/satellite/v1/ws")

    def test_voice_start_becomes_tater_native_envelope(self) -> None:
        frames = TaterNativeClient.frames_for_messages(
            [
                VoiceAssistantRequest(
                    start=True,
                    wake_word_phrase="hey reachy",
                    conversation_id="conversation-1",
                    flags=2,
                )
            ]
        )

        self.assertEqual(len(frames), 1)
        body = _json(frames[0])
        self.assertEqual(body["v"], 1)
        self.assertEqual(body["type"], "voice.start")
        self.assertEqual(
            body["payload"],
            {
                "wake_word": "hey reachy",
                "source": "local_wake",
                "request_flags": 2,
                "audio_format": {"rate": 16000, "width": 2, "channels": 1},
                "conversation_id": "conversation-1",
            },
        )

    def test_audio_and_playback_finished_frames(self) -> None:
        frames = TaterNativeClient.frames_for_messages(
            [
                VoiceAssistantAudio(data=b"\x01\x02"),
                VoiceAssistantAnnounceFinished(),
            ]
        )

        self.assertEqual(frames[0], b"\x01\x02")
        self.assertEqual(_json(frames[1])["type"], "playback.finished")
        self.assertIs(_json(frames[1])["payload"]["ok"], True)

    def test_event_data_matches_lva_string_conventions(self) -> None:
        self.assertEqual(
            _event_data({"continue_conversation": True, "count": 3, "missing": None}),
            {
                "continue_conversation": "1",
                "count": "3",
                "missing": "",
            },
        )

    def test_tater_tool_event_maps_to_lva_intent_progress(self) -> None:
        event_type, data = _voice_event("TOOL_CALL_START", {"tool": "weather"})

        self.assertEqual(event_type, VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS)
        self.assertEqual(data, {"tool": "weather", "type": "tool_call"})


class _FakeState:
    name = "reachy-mini"
    friendly_name = "Reachy Mini"
    version = "test"
    output_only = False
    connected = False
    satellite = None


class _FakeSatellite:
    def __init__(self) -> None:
        self.state = _FakeState()
        self.state.satellite = self
        self.emitted = []
        self.events = []
        self._is_streaming_audio = False
        self._pipeline_active = False
        self._tts_played = False

    def send_messages(self, msgs) -> None:
        del msgs

    def _emit(self, event, data) -> None:
        self.emitted.append((event, data))

    def connection_lost(self, exc) -> None:
        del exc
        self.state.connected = False
        self.state.satellite = None

    def handle_voice_event(self, event_type, data) -> None:
        self.events.append((event_type, data))


class TaterNativeConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_handshake_and_voice_start(self) -> None:
        received = []
        got_start = asyncio.Event()
        got_audio = asyncio.Event()

        async def handler(websocket) -> None:
            hello = json.loads(await websocket.recv())
            received.append(hello)
            await websocket.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "hello.ack",
                        "id": hello["id"],
                        "ts": 1,
                        "payload": {"ok": True, "selector": "native:reachy-mini"},
                    }
                )
            )
            start = json.loads(await websocket.recv())
            received.append(start)
            got_start.set()
            await websocket.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "voice.start.ack",
                        "id": start["id"],
                        "ts": 2,
                        "payload": {"ok": True},
                    }
                )
            )
            received.append(await websocket.recv())
            got_audio.set()
            await websocket.wait_closed()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            satellite = _FakeSatellite()
            client = TaterNativeClient(
                satellite,
                url=f"http://127.0.0.1:{port}",
                device_id="reachy-mini",
                board="reachy_mini",
                heartbeat_seconds=30,
            )
            task = asyncio.create_task(client.run_forever())
            for _ in range(100):
                if client.connected:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(client.connected)

            client.send_messages([VoiceAssistantRequest(start=True, wake_word_phrase="hey reachy")])
            client.send_messages([VoiceAssistantAudio(data=b"\x01\x02")])
            await asyncio.wait_for(got_start.wait(), timeout=2)
            await asyncio.wait_for(got_audio.wait(), timeout=2)
            await client.close()
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(received[0]["type"], "hello")
        self.assertTrue(received[0]["payload"]["capabilities"]["motion"])
        self.assertEqual(received[1]["type"], "voice.start")
        self.assertEqual(received[1]["payload"]["wake_word"], "hey reachy")
        self.assertEqual(received[2], b"\x01\x02")


if __name__ == "__main__":
    unittest.main()
