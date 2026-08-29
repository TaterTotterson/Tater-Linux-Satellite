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
import struct
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import websockets

# pylint: disable=no-name-in-module
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    VoiceAssistantAnnounceFinished,
    VoiceAssistantAudio,
    VoiceAssistantRequest,
)

# pylint: enable=no-name-in-module
from aioesphomeapi.model import VoiceAssistantEventType
from google.protobuf import message

from .peripheral_api import LVAEvent

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_RECONNECT_SECONDS = 2.0
DEFAULT_HEARTBEAT_SECONDS = 5.0
DEFAULT_TTS_SEGMENT_GRACE_SECONDS = 0.65
OUTGOING_QUEUE_MAX = 512
VOICE_PREROLL_MAX_CHUNKS = 64
NATIVE_WS_PATH = "/api/tater/satellite/v1/ws"
WAKE_VERIFIER_MAGIC = b"TWV1"
WAKE_VERIFIER_VERSION = 1
WAKE_VERIFIER_CODEC_PCM16_LE = 1
WAKE_VERIFIER_FLAG_ENFORCE = 0x01
WAKE_VERIFIER_HEADER = struct.Struct("<4sBBHIII")
WAKE_VERIFIER_SAMPLE_RATE = 16000
WAKE_VERIFIER_MIN_CAPTURE_MS = 500
WAKE_VERIFIER_MAX_WINDOW_MS = 2000
WAKE_VERIFIER_MAX_OUTSTANDING = 32


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


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = int(default)
    return max(minimum, min(maximum, number))


def build_wake_verifier_packet(pcm: bytes, *, request_id: int, enforce: bool) -> bytes:
    """Build the binary STT wake-verification packet used by Tater Native."""
    audio = bytes(pcm or b"")
    if len(audio) % 2:
        raise ValueError("wake verifier PCM must contain complete 16-bit samples")
    sample_count = len(audio) // 2
    if sample_count < 1 or sample_count > (WAKE_VERIFIER_SAMPLE_RATE * 2):
        raise ValueError(f"invalid wake verifier sample count: {sample_count}")
    flags = WAKE_VERIFIER_FLAG_ENFORCE if enforce else 0
    return (
        WAKE_VERIFIER_HEADER.pack(
            WAKE_VERIFIER_MAGIC,
            WAKE_VERIFIER_VERSION,
            WAKE_VERIFIER_CODEC_PCM16_LE,
            flags,
            int(request_id) & 0xFFFFFFFF,
            WAKE_VERIFIER_SAMPLE_RATE,
            sample_count,
        )
        + audio
    )


def _websocket_header_options(headers: dict[str, str]) -> dict[str, Any]:
    """Support both legacy and current ``websockets.connect`` header names."""
    try:
        parameters: Any = inspect.signature(websockets.connect).parameters
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
        capabilities: Optional[dict[str, Any]] = None,
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
        self.capabilities: dict[str, Any] = {
            "microphone": not bool(getattr(self.state, "output_only", False)),
            "speaker": True,
            "local_wake": not bool(getattr(self.state, "output_only", False)),
            "continued_chat_reopen": True,
            "barge_in": False,
            "tool_call_mode": True,
            "timers": False,
            "ota": False,
            "motion": self.board.lower().startswith("reachy"),
            "persistent_media_sessions": True,
            "audio_session_version": 1,
            "settings": True,
            "wake_verifier": True,
        }
        if capabilities:
            self.capabilities.update({str(key): value if isinstance(value, (bool, int, float, str)) else bool(value) for key, value in capabilities.items()})

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._websocket: Any = None
        self._connected = False
        self._outgoing: asyncio.Queue[str | bytes] = asyncio.Queue(maxsize=OUTGOING_QUEUE_MAX)
        self._started_monotonic = time.monotonic()
        self._audio_drops = 0
        self._voice_start_pending = False
        self._audio_preroll: deque[bytes] = deque(maxlen=VOICE_PREROLL_MAX_CHUNKS)
        self._wake_audio: deque[bytes] = deque()
        self._wake_audio_bytes = 0
        self._wake_audio_lock = threading.Lock()
        self._wake_verifier_generation = 0
        self._wake_verifier_requests: dict[int, bool] = {}
        self._wake_verifier_pending_id = 0
        self._wake_verifier_pending_word: Any = None
        self._wake_verifier_timeout: Optional[asyncio.TimerHandle] = None
        self._wake_verifier_gate = threading.Event()
        self._wake_verifier_completed = 0
        self._wake_verifier_rejections = 0
        self._wake_verifier_fail_open = 0
        self._wake_verifier_last_reason = ""
        self._media_session_id = ""
        self._media_group_id = ""

        # VoiceSatelliteProtocol already owns the complete local state machine.
        # Replacing only its serializer preserves subclasses such as Reachy's
        # motion-aware protocol hooks.
        self._original_send_messages = satellite.send_messages
        satellite.send_messages = self.send_messages
        self._original_handle_audio = getattr(satellite, "handle_audio", None)
        if callable(self._original_handle_audio):
            satellite.handle_audio = self.handle_audio
        self._original_wakeup = getattr(satellite, "wakeup", None)
        if callable(self._original_wakeup):
            satellite.wakeup = self.wakeup
        configure_tts_segments = getattr(satellite, "set_tts_segment_grace_seconds", None)
        if callable(configure_tts_segments):
            configure_tts_segments(DEFAULT_TTS_SEGMENT_GRACE_SECONDS)

    @property
    def connected(self) -> bool:
        return self._connected

    def report_settings(self, settings: dict[str, Any]) -> None:
        """Report a local hardware setting change to the paired Tater."""
        if not isinstance(settings, dict) or not settings:
            return
        self._submit_frame(
            _json_frame(
                "settings.changed",
                {"ok": True, "settings": dict(settings)},
            )
        )

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

    def _wake_verifier_config(self) -> tuple[str, int, int]:
        settings = dict(getattr(self.state, "native_settings", {}) or {})
        mode = str(settings.get("wake_verifier_mode") or "off").strip().lower()
        if mode not in {"off", "observe", "enforce"}:
            mode = "off"
        window_ms = _bounded_int(
            settings.get("wake_verifier_window_ms"),
            1000,
            minimum=500,
            maximum=WAKE_VERIFIER_MAX_WINDOW_MS,
        )
        timeout_ms = _bounded_int(
            settings.get("wake_verifier_timeout_ms"),
            500,
            minimum=100,
            maximum=2000,
        )
        return mode, window_ms, timeout_ms

    def handle_audio(self, audio_chunk: bytes, audio_chunk_2: Optional[bytes] = None) -> None:
        """Retain enhanced microphone audio before forwarding it normally."""
        frame = bytes(audio_chunk or b"")
        usable = len(frame) - (len(frame) % 2)
        if usable:
            frame = frame[:usable]
            maximum = (WAKE_VERIFIER_SAMPLE_RATE * WAKE_VERIFIER_MAX_WINDOW_MS * 2) // 1000
            with self._wake_audio_lock:
                self._wake_audio.append(frame)
                self._wake_audio_bytes += len(frame)
                while self._wake_audio_bytes > maximum and self._wake_audio:
                    excess = self._wake_audio_bytes - maximum
                    oldest = self._wake_audio[0]
                    if len(oldest) <= excess:
                        self._wake_audio.popleft()
                        self._wake_audio_bytes -= len(oldest)
                    else:
                        trim = excess + (excess % 2)
                        self._wake_audio[0] = oldest[trim:]
                        self._wake_audio_bytes -= trim
        if callable(self._original_handle_audio):
            self._original_handle_audio(audio_chunk, audio_chunk_2)

    def _wake_audio_snapshot(self, window_ms: int) -> bytes:
        requested_bytes = (WAKE_VERIFIER_SAMPLE_RATE * int(window_ms) * 2) // 1000
        minimum_bytes = (WAKE_VERIFIER_SAMPLE_RATE * WAKE_VERIFIER_MIN_CAPTURE_MS * 2) // 1000
        with self._wake_audio_lock:
            if self._wake_audio_bytes < minimum_bytes:
                return b""
            audio = b"".join(self._wake_audio)
        return audio[-requested_bytes:]

    def wakeup(self, wake_word: Any) -> None:
        """Run Tater's second STT check before an enforced local wake."""
        mode, _window_ms, _timeout_ms = self._wake_verifier_config()
        loop = self._loop
        if mode == "off" or not self._connected or loop is None or loop.is_closed():
            if callable(self._original_wakeup):
                self._original_wakeup(wake_word)
            return
        if mode == "enforce":
            if self._wake_verifier_gate.is_set():
                _LOGGER.debug("Ignoring wake while STT wake verification is already pending")
                return
            self._wake_verifier_gate.set()
        loop.call_soon_threadsafe(self._queue_wake_verification, wake_word, mode)
        if mode == "observe" and callable(self._original_wakeup):
            self._original_wakeup(wake_word)

    def _next_wake_verifier_id(self) -> int:
        self._wake_verifier_generation = (self._wake_verifier_generation + 1) & 0xFFFFFFFF
        if self._wake_verifier_generation == 0:
            self._wake_verifier_generation = 1
        return self._wake_verifier_generation

    def _queue_wake_verification(self, wake_word: Any, mode: str) -> None:
        enforce = mode == "enforce"
        if not self._connected:
            self._wake_verifier_gate.clear()
            if enforce and callable(self._original_wakeup):
                self._original_wakeup(wake_word)
            return
        _current_mode, window_ms, timeout_ms = self._wake_verifier_config()
        pcm = self._wake_audio_snapshot(window_ms)
        if not pcm:
            _LOGGER.warning("STT wake verification skipped because the capture ring is not ready")
            self._wake_verifier_gate.clear()
            if enforce and callable(self._original_wakeup):
                self._original_wakeup(wake_word)
            return

        request_id = self._next_wake_verifier_id()
        packet = build_wake_verifier_packet(pcm, request_id=request_id, enforce=enforce)
        while len(self._wake_verifier_requests) >= WAKE_VERIFIER_MAX_OUTSTANDING:
            self._wake_verifier_requests.pop(next(iter(self._wake_verifier_requests)))
        self._wake_verifier_requests[request_id] = enforce
        self._wake_verifier_last_reason = "pending"
        if enforce:
            self._wake_verifier_pending_id = request_id
            self._wake_verifier_pending_word = wake_word

        if not self._queue_frame(packet):
            self._complete_wake_verification(
                request_id,
                accepted=True,
                available=False,
                reason="queue_fail_open",
            )
            return

        _LOGGER.info(
            "STT wake verification queued request=%d mode=%s samples=%d timeout_ms=%d",
            request_id,
            mode,
            len(pcm) // 2,
            timeout_ms,
        )
        if enforce:
            loop = self._loop
            if loop is None or loop.is_closed():
                self._complete_wake_verification(
                    request_id,
                    accepted=True,
                    available=False,
                    reason="timer_unavailable_fail_open",
                )
                return
            self._wake_verifier_timeout = loop.call_later(
                timeout_ms / 1000.0,
                self._wake_verifier_timed_out,
                request_id,
            )

    def _wake_verifier_timed_out(self, request_id: int) -> None:
        self._complete_wake_verification(
            request_id,
            accepted=True,
            available=False,
            reason="satellite_timeout_fail_open",
        )

    def _complete_wake_verification(
        self,
        request_id: int,
        *,
        accepted: bool,
        available: bool,
        reason: str,
    ) -> None:
        enforce = self._wake_verifier_requests.pop(request_id, None)
        if enforce is None:
            return
        fail_open = not available
        self._wake_verifier_completed += 1
        if not accepted and not fail_open:
            self._wake_verifier_rejections += 1
        if fail_open:
            self._wake_verifier_fail_open += 1
        self._wake_verifier_last_reason = str(reason or ("accepted" if accepted else "rejected"))

        pending_wake = None
        if enforce and request_id == self._wake_verifier_pending_id:
            timeout = self._wake_verifier_timeout
            self._wake_verifier_timeout = None
            if timeout is not None:
                timeout.cancel()
            pending_wake = self._wake_verifier_pending_word
            self._wake_verifier_pending_id = 0
            self._wake_verifier_pending_word = None
            self._wake_verifier_gate.clear()

        _LOGGER.info(
            "STT wake verification result request=%d accepted=%s available=%s enforced=%s reason=%s",
            request_id,
            accepted,
            available,
            bool(enforce),
            self._wake_verifier_last_reason,
        )
        if pending_wake is None:
            return
        if accepted or fail_open:
            if callable(self._original_wakeup):
                self._original_wakeup(pending_wake)
        else:
            _LOGGER.info("STT wake verification rejected false wake")

    def _wake_verifier_status(self) -> dict[str, Any]:
        return {
            "pending": bool(self._wake_verifier_pending_id),
            "pending_id": self._wake_verifier_pending_id,
            "completed": self._wake_verifier_completed,
            "rejections": self._wake_verifier_rejections,
            "fail_open": self._wake_verifier_fail_open,
            "last_reason": self._wake_verifier_last_reason,
        }

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

    def _queue_frame(self, frame: str | bytes) -> bool:
        if not self._connected:
            return False
        try:
            self._outgoing.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            if isinstance(frame, bytes):
                self._audio_drops += 1
                if self._audio_drops == 1 or self._audio_drops % 100 == 0:
                    _LOGGER.warning("Tater native audio queue full; dropped %d chunks", self._audio_drops)
                return False
            try:
                self._outgoing.get_nowait()
                self._outgoing.task_done()
            except asyncio.QueueEmpty:
                pass
            self._outgoing.put_nowait(frame)
            return True

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
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
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
        timeout = self._wake_verifier_timeout
        self._wake_verifier_timeout = None
        if timeout is not None:
            timeout.cancel()
        self._wake_verifier_requests.clear()
        self._wake_verifier_pending_id = 0
        self._wake_verifier_pending_word = None
        self._wake_verifier_gate.clear()
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
                tts_response_active = bool(getattr(self.satellite, "tts_response_active", False))
                state = "speaking" if tts_response_active or bool(getattr(self.satellite, "_tts_played", False)) else "thinking"
            elif self._media_session_id:
                state = "playing"
            self._queue_frame(
                _json_frame(
                    "status",
                    {
                        "state": state,
                        "uptime_s": int(time.monotonic() - self._started_monotonic),
                        "connected": True,
                        "audio_tx_dropped": self._audio_drops,
                        "volume_percent": int(round(max(0.0, min(1.0, float(self.state.volume))) * 100)),
                        "wake_engine": {"verifier": self._wake_verifier_status()},
                    },
                )
            )

    def _handle_message(self, body: dict[str, Any]) -> None:
        message_type = str(body.get("type") or "").strip()
        raw_payload = body.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        if message_type == "settings":
            from .live_settings import apply_live_settings

            try:
                applied = apply_live_settings(self.state, payload)
                result = {"ok": True, "settings": applied}
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.exception("Could not apply Tater live settings")
                result = {
                    "ok": False,
                    "error": str(exc),
                    "settings": dict(getattr(self.state, "native_settings", {}) or {}),
                }
            self._submit_frame(_json_frame("settings.changed", result, message_id=str(body.get("id") or "")))
            return

        if message_type == "voice.event":
            event_type, data = _voice_event(payload.get("event"), payload.get("data"))
            if event_type is not None:
                self.satellite.handle_voice_event(event_type, data)
            else:
                _LOGGER.debug("Ignoring unsupported Tater voice event: %s", payload.get("event"))
            return

        if message_type == "state":
            # Tater sends these visual states immediately. In particular,
            # ``thinking`` arrives at VAD end, before remote STT/LLM/TTS work;
            # waiting for INTENT_START can make the animation visible for only
            # a fraction of a second. Keep playback-driven speaking events in
            # the existing TTS player path so the reply ring starts with sound.
            visual_state = str(payload.get("state") or "").strip().lower()
            if visual_state == "thinking":
                self.satellite._emit(LVAEvent.THINKING, None)  # pylint: disable=protected-access
            elif visual_state == "tool_call":
                self.satellite._emit(LVAEvent.TOOL_CALL, payload)  # pylint: disable=protected-access
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

        if message_type == "wake.verify.result":
            try:
                request_id = int(payload.get("request_id") or 0)
            except (TypeError, ValueError):
                request_id = 0
            self._complete_wake_verification(
                request_id,
                accepted=_truthy(payload.get("accepted")),
                available=("available" not in payload or _truthy(payload.get("available"))),
                reason=str(payload.get("reason") or ""),
            )
            return

        if message_type == "media.session.start":
            self._start_media_session(payload)
            return

        if message_type == "media.session.stop":
            self._stop_media_session(payload)
            return

        if message_type == "media.session.pause":
            self._control_media_session(payload, "pause")
            return

        if message_type == "media.session.resume":
            self._control_media_session(payload, "resume")
            return

        if message_type == "media.session.volume":
            self._set_media_session_volume(payload)
            return

        if message_type == "play.url":
            url = str(payload.get("url") or "").strip()
            if not url:
                return
            if self._media_session_id:
                self._duck_media_for_speech(payload)
            if hasattr(self.satellite, "_reachy_tts_kind"):
                self.satellite._reachy_tts_kind = str(payload.get("tts_kind") or "")  # pylint: disable=protected-access
            if str(payload.get("tts_kind") or "").strip().lower() in {"tool", "tool_progress"} and hasattr(self.satellite, "_reachy_tool_progress_active"):
                self.satellite._reachy_tool_progress_active = True  # pylint: disable=protected-access
            queue_tts_segment = getattr(self.satellite, "queue_tts_segment", None)
            if callable(queue_tts_segment):
                queue_tts_segment(
                    url,
                    continue_conversation=_truthy(payload.get("continue_conversation")),
                )
            else:
                self.satellite._tts_url = url  # pylint: disable=protected-access
                self.satellite._tts_played = False  # pylint: disable=protected-access
                self.satellite._continue_conversation = _truthy(payload.get("continue_conversation"))  # pylint: disable=protected-access
                self.satellite.play_tts()
            return

        if message_type == "play.tone":
            sound = str(getattr(self.state, "timer_finished_sound", "") or "").strip()
            if sound:
                self.state.tts_player.play(sound, done_callback=lambda: self._submit_frame(_json_frame("playback.finished", {"ok": True})))
            return

        if message_type == "play.stop":
            self.satellite.stop()
            return

        if message_type == "error":
            _LOGGER.error("Tater native satellite error: %s", payload.get("error") or payload.get("message") or "unknown error")

    def _start_media_session(self, payload: dict[str, Any]) -> None:
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        session_id = str(payload.get("session_id") or "").strip()
        group_id = str(payload.get("group_id") or "").strip()
        media_url = str(media.get("url") or payload.get("url") or "").strip()
        if not session_id or not media_url:
            self._submit_media_event(
                "media.session.finished",
                session_id=session_id,
                group_id=group_id,
                ok=False,
                reason="session_id and media.url are required",
            )
            return

        player = getattr(self.state, "music_player", None)
        play_persistent = getattr(player, "play_persistent", None)
        play = getattr(player, "play", None)
        if not callable(play_persistent) and not callable(play):
            self._submit_media_event(
                "media.session.finished",
                session_id=session_id,
                group_id=group_id,
                ok=False,
                reason="Persistent media player is unavailable",
            )
            return

        previous_session_id = self._media_session_id
        if previous_session_id and previous_session_id != session_id:
            try:
                player.stop()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Could not stop the previous native media session", exc_info=True)

        self._media_session_id = session_id
        self._media_group_id = group_id
        raw_volume = media.get("volume_percent")
        try:
            volume = max(0.0, min(100.0, float(100.0 if raw_volume is None else raw_volume)))
        except (TypeError, ValueError):
            volume = 100.0
        player.set_volume(volume)

        def media_event(event: str, detail: str) -> None:
            if event == "started":
                self._media_session_id = session_id
                self._media_group_id = group_id
                self._submit_media_event(
                    "media.session.started",
                    session_id=session_id,
                    group_id=group_id,
                    ok=True,
                )
                return
            self._submit_media_event(
                "media.session.finished",
                session_id=session_id,
                group_id=group_id,
                ok=event != "error",
                reason=detail,
            )
            if self._media_session_id == session_id:
                self._media_session_id = ""
                self._media_group_id = ""

        try:
            try:
                start_position_ms = max(0, int(float(media.get("start_position_ms") or 0)))
            except (TypeError, ValueError):
                start_position_ms = 0
            loop = _truthy(media.get("loop"))
            if callable(play_persistent):
                play_persistent(
                    media_url,
                    start_position_ms=start_position_ms,
                    loop=loop,
                    event_callback=media_event,
                )
            else:
                play(
                    media_url,
                    done_callback=lambda: media_event("finished", ""),
                )
                media_event("started", "")
        except Exception as exc:  # pylint: disable=broad-except
            self._media_session_id = ""
            self._media_group_id = ""
            self._submit_media_event(
                "media.session.finished",
                session_id=session_id,
                group_id=group_id,
                ok=False,
                reason=str(exc).strip() or "Could not start native music playback",
            )

    def _stop_media_session(self, payload: dict[str, Any]) -> None:
        active_session_id = self._media_session_id
        if not active_session_id:
            return
        requested_session_id = str(payload.get("session_id") or "").strip()
        if requested_session_id and requested_session_id != active_session_id:
            return
        player = getattr(self.state, "music_player", None)
        stop = getattr(player, "stop", None)
        if callable(stop):
            stop()

    def _control_media_session(self, payload: dict[str, Any], action: str) -> None:
        active_session_id = self._media_session_id
        if not active_session_id:
            return
        requested_session_id = str(payload.get("session_id") or "").strip()
        if requested_session_id and requested_session_id != active_session_id:
            return
        player = getattr(self.state, "music_player", None)
        control = getattr(player, action, None)
        if callable(control):
            control()

    def _set_media_session_volume(self, payload: dict[str, Any]) -> None:
        active_session_id = self._media_session_id
        if not active_session_id:
            return
        requested_session_id = str(payload.get("session_id") or "").strip()
        if requested_session_id and requested_session_id != active_session_id:
            return
        try:
            volume = max(0.0, min(100.0, float(payload.get("volume_percent") or 0.0)))
        except (TypeError, ValueError):
            return
        player = getattr(self.state, "music_player", None)
        set_volume = getattr(player, "set_volume", None)
        if callable(set_volume):
            set_volume(volume)

    def _duck_media_for_speech(self, payload: dict[str, Any]) -> None:
        ducking = payload.get("ducking") if isinstance(payload.get("ducking"), dict) else {}
        raw_target = ducking.get("target_percent")
        try:
            factor = max(0.0, min(1.0, float(50.0 if raw_target is None else raw_target) / 100.0))
        except (TypeError, ValueError):
            factor = 0.5
        player = getattr(self.state, "music_player", None)
        duck = getattr(player, "duck", None)
        if callable(duck):
            duck(factor)

    def _submit_media_event(
        self,
        message_type: str,
        *,
        session_id: str,
        group_id: str,
        ok: bool,
        reason: str = "",
    ) -> None:
        event_payload: dict[str, Any] = {
            "session_id": session_id,
            "group_id": group_id,
            "ok": bool(ok),
        }
        if message_type == "media.session.started":
            actual_start_us = time.monotonic_ns() // 1000
            event_payload.update(
                {
                    "channel": "stereo",
                    "sample_rate_hz": 48000,
                    "scheduled_start_us": actual_start_us,
                    "actual_start_us": actual_start_us,
                    "late_by_us": 0,
                }
            )
        if reason:
            event_payload["reason"] = reason
        self._submit_frame(_json_frame(message_type, event_payload))

    async def close(self) -> None:
        stop = self._stop
        if stop is not None:
            stop.set()
        websocket = self._websocket
        if websocket is not None:
            await websocket.close()
        self._mark_disconnected(None)
