"""Tater native satellite WebSocket transport.

This transport lets Linux Voice Assistant act as a client of Tater's native
satellite API while reusing the existing wake-word, audio, playback, timer,
and peripheral state machine from :mod:`linux_voice_assistant.satellite`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import websockets
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    VoiceAssistantAnnounceFinished,
    VoiceAssistantAudio,
    VoiceAssistantRequest,
)
from aioesphomeapi.model import VoiceAssistantEventType
from google.protobuf import message

from .peripheral_api import LVAEvent

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_RECONNECT_SECONDS = 2.0
DEFAULT_HEARTBEAT_SECONDS = 5.0
OUTGOING_QUEUE_MAX = 512
VOICE_PREROLL_MAX_CHUNKS = 64
NATIVE_WS_PATH = "/api/tater/satellite/v1/ws"


def _envelope(message_type: str, payload: Optional[dict[str, Any]] = None, *, message_id: str = "") -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": str(message_type or "").strip(),
        "id": message_id or uuid.uuid4().hex,
        "ts": time.time(),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _json_frame(message_type: str, payload: Optional[dict[str, Any]] = None, *, message_id: str = "") -> str:
    return json.dumps(_envelope(message_type, payload, message_id=message_id), separators=(",", ":"))


def normalize_tater_url(value: Any) -> str:
    """Accept a Tater base URL or a complete native WebSocket URL."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"ws://{raw}"
    parsed = urlparse(raw)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme.lower(), parsed.scheme.lower())
    path = parsed.path.rstrip("/")
    if NATIVE_WS_PATH not in path:
        path = f"{path}{NATIVE_WS_PATH}" if path else NATIVE_WS_PATH
    return urlunparse((scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _websocket_header_options(headers: dict[str, str]) -> dict[str, dict[str, str]]:
    """Support both legacy and current ``websockets.connect`` header names."""
    try:
        parameters = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        parameters = {}
    header_argument = "additional_headers" if "additional_headers" in parameters else "extra_headers"
    return {header_argument: headers}


def _event_data(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, bool):
            normalized[str(key)] = "1" if value else "0"
        elif value is None:
            normalized[str(key)] = ""
        elif isinstance(value, (dict, list)):
            normalized[str(key)] = json.dumps(value, separators=(",", ":"))
        else:
            normalized[str(key)] = str(value)
    return normalized


def _voice_event(event_name: Any, data: Any) -> tuple[Optional[VoiceAssistantEventType], dict[str, str]]:
    token = str(event_name or "").strip().upper()
    if token.startswith("VOICE_ASSISTANT_"):
        token = token[len("VOICE_ASSISTANT_") :]
    normalized = _event_data(data)

    # Tater has explicit tool lifecycle events that do not exist in the
    # aioesphomeapi enum. INTENT_PROGRESS is the closest lossless LVA event and
    # lets Reachy-style renderers recognize the tool phase from the metadata.
    if token == "TOOL_CALL_START":
        normalized.setdefault("type", "tool_call")
        return VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS, normalized
    if token == "TOOL_CALL_END":
        normalized.setdefault("type", "tool_call_end")
        return VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_START, normalized

    enum_name = f"VOICE_ASSISTANT_{token}"
    return getattr(VoiceAssistantEventType, enum_name, None), normalized


class TaterNativeClient:
    """Connect an existing ``VoiceSatelliteProtocol`` to Tater over WebSocket."""

    def __init__(
        self,
        satellite: Any,
        *,
        url: str,
        token: str = "",
        token_file: str | Path | None = None,
        device_id: str = "",
        device_name: str = "",
        board: str = "linux",
        room: str = "",
        firmware_version: str = "",
        reconnect_seconds: float = DEFAULT_RECONNECT_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        capabilities: Optional[dict[str, bool]] = None,
    ) -> None:
        self.satellite = satellite
        self.state = satellite.state
        self.url = normalize_tater_url(url)
        if not self.url:
            raise ValueError("Tater native WebSocket URL is required")

        self.token_file = Path(token_file).expanduser() if token_file else None
        self.token = self._load_saved_token() or str(token or "").strip()
        self.device_id = str(device_id or getattr(self.state, "name", "") or "linux-voice-assistant").strip()
        self.device_name = str(device_name or getattr(self.state, "friendly_name", "") or self.device_id).strip()
        self.board = str(board or "linux").strip()
        self.room = str(room or "").strip()
        self.firmware_version = str(firmware_version or getattr(self.state, "version", "") or "unknown").strip()
        self.reconnect_seconds = max(0.25, float(reconnect_seconds or DEFAULT_RECONNECT_SECONDS))
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds or DEFAULT_HEARTBEAT_SECONDS))
        self.capabilities = {
            "microphone": not bool(getattr(self.state, "output_only", False)),
            "speaker": True,
            "local_wake": not bool(getattr(self.state, "output_only", False)),
            "continued_chat_reopen": True,
            "barge_in": False,
            "tool_call_mode": True,
            "timers": False,
            "ota": False,
            "motion": self.board.lower().startswith("reachy"),
        }
        if capabilities:
            self.capabilities.update({str(key): bool(value) for key, value in capabilities.items()})

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._websocket: Any = None
        self._connected = False
        self._outgoing: asyncio.Queue[str | bytes] = asyncio.Queue(maxsize=OUTGOING_QUEUE_MAX)
        self._started_monotonic = time.monotonic()
        self._audio_drops = 0
        self._voice_start_pending = False
        self._audio_preroll: deque[bytes] = deque(maxlen=VOICE_PREROLL_MAX_CHUNKS)

        # VoiceSatelliteProtocol already owns the complete local state machine.
        # Replacing only its serializer preserves subclasses such as Reachy's
        # motion-aware protocol hooks.
        self._original_send_messages = satellite.send_messages
        satellite.send_messages = self.send_messages

    @property
    def connected(self) -> bool:
        return self._connected

    def _load_saved_token(self) -> str:
        if self.token_file is None:
            return ""
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            _LOGGER.warning("Unable to read Tater token file %s: %s", self.token_file, exc)
            return ""

    def _save_token(self, token: Any) -> None:
        value = str(token or "").strip()
        if not value or self.token_file is None:
            return
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(value + "\n", encoding="utf-8")
            os.chmod(self.token_file, 0o600)
        except OSError as exc:
            _LOGGER.warning("Unable to save paired Tater token to %s: %s", self.token_file, exc)

    def _hello(self) -> str:
        return _json_frame(
            "hello",
            {
                "device_id": self.device_id,
                "device_name": self.device_name,
                "board": self.board,
                "firmware_version": self.firmware_version,
                "room": self.room,
                "capabilities": self.capabilities,
            },
        )

    def _headers(self) -> dict[str, str]:
        return {"X-Tater-Token": self.token} if self.token else {}

    def send_messages(self, msgs: Iterable[message.Message]) -> None:
        messages = list(msgs or [])
        if not messages:
            return
        if not self._connected:
            if any(isinstance(msg, VoiceAssistantRequest) and bool(msg.start) for msg in messages):
                self.satellite._is_streaming_audio = False  # pylint: disable=protected-access
                self.satellite._pipeline_active = False  # pylint: disable=protected-access
                _LOGGER.warning("Ignoring voice start while disconnected from Tater")
            return

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for protocol_message in messages:
            loop.call_soon_threadsafe(self._queue_protocol_message, protocol_message)

    def _queue_protocol_message(self, protocol_message: message.Message) -> None:
        if not self._connected:
            return
        if isinstance(protocol_message, VoiceAssistantRequest):
            if protocol_message.start:
                self._voice_start_pending = True
                self._audio_preroll.clear()
            else:
                self._voice_start_pending = False
                self._audio_preroll.clear()

        for frame in self.frames_for_messages([protocol_message]):
            if isinstance(frame, bytes) and self._voice_start_pending:
                if len(self._audio_preroll) == self._audio_preroll.maxlen:
                    self._audio_drops += 1
                self._audio_preroll.append(frame)
                continue
            self._queue_frame(frame)

    @staticmethod
    def frames_for_messages(msgs: Iterable[message.Message]) -> list[str | bytes]:
        frames: list[str | bytes] = []
        for msg in msgs:
            if isinstance(msg, VoiceAssistantAudio):
                if msg.data:
                    frames.append(bytes(msg.data))
                if bool(getattr(msg, "end", False)):
                    frames.append(_json_frame("voice.stop", {"abort": False}))
                continue

            if isinstance(msg, VoiceAssistantRequest):
                if msg.start:
                    payload: dict[str, Any] = {
                        "wake_word": str(msg.wake_word_phrase or ""),
                        "source": "local_wake" if msg.wake_word_phrase else "continued_chat",
                        "request_flags": int(msg.flags or 0),
                        "audio_format": {"rate": 16000, "width": 2, "channels": 1},
                    }
                    if msg.conversation_id:
                        payload["conversation_id"] = str(msg.conversation_id)
                    frames.append(_json_frame("voice.start", payload))
                else:
                    frames.append(_json_frame("voice.stop", {"abort": False}))
                continue

            if isinstance(msg, VoiceAssistantAnnounceFinished):
                frames.append(_json_frame("playback.finished", {"ok": True}))
        return frames

    def _submit_frame(self, frame: str | bytes) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue_frame, frame)

    def _queue_frame(self, frame: str | bytes) -> None:
        if not self._connected:
            return
        try:
            self._outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            if isinstance(frame, bytes):
                self._audio_drops += 1
                if self._audio_drops == 1 or self._audio_drops % 100 == 0:
                    _LOGGER.warning("Tater native audio queue full; dropped %d chunks", self._audio_drops)
                return
            try:
                self._outgoing.get_nowait()
                self._outgoing.task_done()
            except asyncio.QueueEmpty:
                pass
            self._outgoing.put_nowait(frame)

    def _clear_outgoing(self) -> None:
        while True:
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._outgoing.task_done()

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        while not self._stop.is_set():
            error: Optional[BaseException] = None
            try:
                async with websockets.connect(
                    self.url,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                    **_websocket_header_options(self._headers()),
                ) as websocket:
                    self._websocket = websocket
                    await self._run_connection(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                error = exc
                _LOGGER.warning("Tater native connection failed (%s); retrying in %.1fs", exc, self.reconnect_seconds)
            finally:
                self._websocket = None
                self._mark_disconnected(error)

            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.reconnect_seconds)
                except asyncio.TimeoutError:
                    pass

    async def _run_connection(self, websocket: Any) -> None:
        self._clear_outgoing()
        await websocket.send(self._hello())
        first = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        if not isinstance(first, str):
            raise RuntimeError("Tater did not return a JSON hello acknowledgement")
        first_message = json.loads(first)
        first_type = ""
        first_payload: dict[str, Any] = {}
        if isinstance(first_message, dict):
            first_type = str(first_message.get("type") or "")
            raw_payload = first_message.get("payload")
            if isinstance(raw_payload, dict):
                first_payload = raw_payload
        if first_type == "error":
            raise RuntimeError(str(first_payload.get("error") or "Tater rejected the satellite connection"))
        if first_type != "hello.ack" or not _truthy(first_payload.get("ok")):
            raise RuntimeError(f"Expected hello.ack from Tater, received {first_type or 'unknown'}")

        paired_token = str(first_payload.get("device_token") or "").strip()
        if paired_token:
            self.token = paired_token
            self._save_token(paired_token)

        self._mark_connected(first_payload)
        reader = asyncio.create_task(self._reader(websocket))
        writer = asyncio.create_task(self._writer(websocket))
        heartbeat = asyncio.create_task(self._heartbeat())
        stop_wait = asyncio.create_task(self._stop.wait()) if self._stop is not None else None
        tasks: set[asyncio.Task[Any]] = {reader, writer, heartbeat}
        if stop_wait is not None:
            tasks.add(stop_wait)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is not stop_wait:
                task.result()

    def _mark_connected(self, ack_payload: dict[str, Any]) -> None:
        self._connected = True
        self.state.connected = True
        self.state.satellite = self.satellite
        self.satellite._emit(LVAEvent.ZEROCONF, {"status": "connected"})  # pylint: disable=protected-access
        _LOGGER.info(
            "Connected to Tater native satellite API selector=%s device_id=%s board=%s room=%s",
            ack_payload.get("selector") or f"native:{self.device_id}",
            self.device_id,
            self.board,
            self.room or "-",
        )

    def _mark_disconnected(self, error: Optional[BaseException]) -> None:
        was_connected = self._connected
        self._connected = False
        self._voice_start_pending = False
        self._audio_preroll.clear()
        self._clear_outgoing()
        if not was_connected:
            return
        try:
            self.satellite.connection_lost(error)
        finally:
            # VoiceSatelliteProtocol normally gets reconstructed by the ESPHome
            # server after a disconnect. Native mode reconnects the same object.
            self.state.satellite = self.satellite
            self.state.connected = False

    async def _reader(self, websocket: Any) -> None:
        async for raw in websocket:
            if not isinstance(raw, str):
                continue
            try:
                message_body = json.loads(raw)
            except json.JSONDecodeError:
                _LOGGER.warning("Ignoring invalid JSON from Tater")
                continue
            if isinstance(message_body, dict):
                self._handle_message(message_body)

    async def _writer(self, websocket: Any) -> None:
        while True:
            frame = await self._outgoing.get()
            try:
                await websocket.send(frame)
            finally:
                self._outgoing.task_done()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            state = "idle"
            if bool(getattr(self.satellite, "_is_streaming_audio", False)):
                state = "listening"
            elif bool(getattr(self.satellite, "_pipeline_active", False)):
                state = "speaking" if bool(getattr(self.satellite, "_tts_played", False)) else "thinking"
            self._queue_frame(
                _json_frame(
                    "status",
                    {
                        "state": state,
                        "uptime_s": int(time.monotonic() - self._started_monotonic),
                        "connected": True,
                        "audio_tx_dropped": self._audio_drops,
                    },
                )
            )

    def _handle_message(self, body: dict[str, Any]) -> None:
        message_type = str(body.get("type") or "").strip()
        raw_payload = body.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        if message_type == "voice.event":
            event_type, data = _voice_event(payload.get("event"), payload.get("data"))
            if event_type is not None:
                self.satellite.handle_voice_event(event_type, data)
            else:
                _LOGGER.debug("Ignoring unsupported Tater voice event: %s", payload.get("event"))
            return

        if message_type == "voice.start.ack":
            if not _truthy(payload.get("ok")):
                self._voice_start_pending = False
                self._audio_preroll.clear()
                self.satellite._is_streaming_audio = False  # pylint: disable=protected-access
                self.satellite._pipeline_active = False  # pylint: disable=protected-access
                _LOGGER.error("Tater rejected voice start: %s", payload.get("error") or payload.get("result") or "unknown error")
            else:
                self._voice_start_pending = False
                while self._audio_preroll:
                    self._queue_frame(self._audio_preroll.popleft())
            return

        if message_type == "play.url":
            url = str(payload.get("url") or "").strip()
            if not url:
                return
            if str(getattr(self.satellite, "_tts_url", "") or "") == url and bool(getattr(self.satellite, "_tts_played", False)):
                return
            self.satellite._tts_url = url  # pylint: disable=protected-access
            self.satellite._tts_played = False  # pylint: disable=protected-access
            self.satellite._continue_conversation = _truthy(payload.get("continue_conversation"))  # pylint: disable=protected-access
            if hasattr(self.satellite, "_reachy_tts_kind"):
                self.satellite._reachy_tts_kind = str(payload.get("tts_kind") or "")  # pylint: disable=protected-access
            if str(payload.get("tts_kind") or "").strip().lower() in {"tool", "tool_progress"} and hasattr(self.satellite, "_reachy_tool_progress_active"):
                self.satellite._reachy_tool_progress_active = True  # pylint: disable=protected-access
            self.satellite.play_tts()
            return

        if message_type == "play.tone":
            sound = str(getattr(self.state, "timer_finished_sound", "") or "").strip()
            if sound:
                self.state.tts_player.play(sound, done_callback=lambda: self._submit_frame(_json_frame("playback.finished", {"ok": True})))
            return

        if message_type == "play.stop":
            self.state.tts_player.stop()
            return

        if message_type == "error":
            _LOGGER.error("Tater native satellite error: %s", payload.get("error") or payload.get("message") or "unknown error")

    async def close(self) -> None:
        stop = self._stop
        if stop is not None:
            stop.set()
        websocket = self._websocket
        if websocket is not None:
            await websocket.close()
        self._mark_disconnected(None)
