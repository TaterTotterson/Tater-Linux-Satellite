"""Apply Tater Native live settings to the Linux voice runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .models import AvailableWakeWord, WakeWordType

_LOGGER = logging.getLogger(__name__)

_SOUNDS_DIR = Path(__file__).resolve().parent / "assets" / "tater_native"
_MAX_WAKE_CONFIG_BYTES = 64 * 1024
_MAX_WAKE_MODEL_BYTES = 4 * 1024 * 1024
_MAX_WAKE_SOUND_BYTES = 4 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 8.0

_WAKE_SOUND_ALIASES = {
    "default": "wake_word_triggered",
    "no-sound": "no_sound",
    "none": "no_sound",
    "off": "no_sound",
}

_BUILTIN_WAKE_SOUNDS = {
    "blip2",
    "message-notification-4",
    "notification-ding",
    "notification-squeak",
    "phone-chime",
    "pop-up-sound",
    "short-definite-fart",
    "star_treck_communications_start_transmission",
    "star_treck_computer_work_beep",
    "tater_notify_digital_blip",
    "turning-off-microphone-percussion-1",
    "wake_word_triggered",
    "waterdrop",
}

_LED_ANIMATIONS = {
    "directional",
    "sparkle",
    "ping_pong",
    "voice_ring",
    "spinner",
    "orbit",
    "pulse",
    "breathe",
    "comet",
    "dual_comet",
    "scanner",
    "ripple",
    "heartbeat",
    "theater",
    "wave",
    "shimmer",
    "twinkle",
    "equalizer",
    "solid",
}

_LED_DEFAULTS = {
    "led_brightness": 80,
    "led_color": "#ff5a1f",
    "led_listening_animation": "directional",
    "led_thinking_animation": "sparkle",
    "led_tool_call_animation": "ping_pong",
    "led_replying_animation": "voice_ring",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = _text(value).lower()
    if token in {"1", "true", "yes", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    return round(_bounded_float(value, float(default), float(minimum), float(maximum)))


def _http_url(value: Any, *, label: str) -> str:
    url = _text(value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an http:// or https:// URL")
    return url


def _download(url: str, *, maximum: int, label: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Tater-Linux-Satellite/1"})
    with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = _http_url(response.geturl(), label=label)
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > maximum:
            raise ValueError(f"{label} is larger than {maximum} bytes")
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError(f"{label} is larger than {maximum} bytes")
    if not body:
        raise ValueError(f"{label} download was empty: {final_url}")
    return body


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _find_wake_word(state: Any, requested_id: str) -> tuple[str, AvailableWakeWord] | None:
    exact = state.available_wake_words.get(requested_id)
    if exact is not None:
        return requested_id, exact
    prefix = f"{requested_id}_"
    for candidate_id, candidate in state.available_wake_words.items():
        if candidate_id.startswith(prefix):
            return candidate_id, candidate
    return None


def _custom_wake_word(state: Any, source_url: str, *, allow_download: bool) -> tuple[str, AvailableWakeWord]:
    url = _http_url(source_url, label="wake-word JSON URL")
    model_id = f"tater_custom_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"
    directory = Path(state.download_dir) / "tater_native_wake_words"
    config_path = directory / f"{model_id}.json"
    model_path = directory / f"{model_id}.tflite"

    if not (config_path.is_file() and model_path.is_file()):
        if not allow_download:
            raise FileNotFoundError("cached custom wake word is unavailable")
        config_body = _download(url, maximum=_MAX_WAKE_CONFIG_BYTES, label="wake-word JSON")
        try:
            config = json.loads(config_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("wake-word JSON is invalid") from exc
        if not isinstance(config, dict) or _text(config.get("type")) != "micro":
            raise ValueError("custom wake word must use the micro model type")
        model_reference = _text(config.get("model") or config.get("model_url"))
        if not model_reference:
            raise ValueError("wake-word JSON does not include a model")
        model_url = _http_url(urljoin(url, model_reference), label="wake-word model URL")
        model_body = _download(model_url, maximum=_MAX_WAKE_MODEL_BYTES, label="wake-word model")
        local_config = dict(config)
        local_config["model"] = model_path.name
        _atomic_write(model_path, model_body)
        _atomic_write(config_path, (json.dumps(local_config, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    micro = config.get("micro") if isinstance(config.get("micro"), dict) else {}
    return model_id, AvailableWakeWord(
        id=model_id,
        type=WakeWordType.MICRO_WAKE_WORD,
        wake_word=_text(config.get("wake_word") or config.get("label") or "Custom Wake Word"),
        trained_languages=[str(language) for language in config.get("trained_languages", [])],
        wake_word_path=config_path,
        probability_cutoff=_bounded_float(micro.get("probability_cutoff"), 0.7, 0.01, 0.99),
    )


def _prepare_wake_word(state: Any, settings: dict[str, Any], *, allow_download: bool) -> tuple[str, str, AvailableWakeWord | None, Any]:
    engine = _text(settings.get("wake_engine") or "micro_wake_word").lower()
    if engine not in {"off", "button", "micro_wake_word", "server"}:
        engine = "micro_wake_word"
    if engine != "micro_wake_word":
        return engine, "", None, None

    requested = _text(settings.get("wake_word") or "hey_tater").lower().replace("-", "_")
    if requested == "custom_url":
        model_id, available = _custom_wake_word(
            state,
            _text(settings.get("wake_word_url")),
            allow_download=allow_download,
        )
    else:
        found = _find_wake_word(state, requested)
        if found is None:
            raise ValueError(f"wake word is not installed: {requested}")
        model_id, available = found

    current = state.wake_words.get(model_id)
    model = current if current is not None else available.load()
    return engine, model_id, available, model


def _prepare_wake_sound(state: Any, settings: dict[str, Any], *, allow_download: bool) -> str:
    enabled = _truthy(settings.get("wake_sound_enabled"), False)
    sound_token = _text(settings.get("wake_sound") or "no_sound").lower()
    sound_id = _WAKE_SOUND_ALIASES.get(
        sound_token,
        _WAKE_SOUND_ALIASES.get(sound_token.replace("_", "-"), sound_token),
    )
    if not enabled or sound_id == "no_sound":
        return ""
    if sound_id == "custom":
        url = _http_url(settings.get("wake_sound_url"), label="wake-sound URL")
        path = Path(state.download_dir) / "tater_native_sounds" / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}.wav"
        if not path.is_file():
            if not allow_download:
                raise FileNotFoundError("cached custom wake sound is unavailable")
            _atomic_write(path, _download(url, maximum=_MAX_WAKE_SOUND_BYTES, label="wake sound"))
        return str(path)
    if sound_id not in _BUILTIN_WAKE_SOUNDS:
        raise ValueError(f"unknown wake sound: {sound_id}")
    path = _SOUNDS_DIR / f"{sound_id}.wav"
    if not path.is_file():
        raise FileNotFoundError(f"built-in wake sound is missing: {sound_id}")
    return str(path)


def _normalize_led_settings(settings: dict[str, Any]) -> None:
    settings["led_brightness"] = _bounded_int(
        settings.get("led_brightness"), int(_LED_DEFAULTS["led_brightness"]), 0, 100
    )
    color = _text(settings.get("led_color") or _LED_DEFAULTS["led_color"]).lower()
    color = color.removeprefix("#")
    if len(color) == 3 and all(character in "0123456789abcdef" for character in color):
        color = "".join(character * 2 for character in color)
    if len(color) != 6 or any(character not in "0123456789abcdef" for character in color):
        color = str(_LED_DEFAULTS["led_color"])[1:]
    settings["led_color"] = f"#{color}"
    for key in (
        "led_listening_animation",
        "led_thinking_animation",
        "led_tool_call_animation",
        "led_replying_animation",
    ):
        animation = _text(settings.get(key) or _LED_DEFAULTS[key]).lower().replace("-", "_")
        settings[key] = animation if animation in _LED_ANIMATIONS else _LED_DEFAULTS[key]


def apply_live_settings(
    state: Any,
    payload: dict[str, Any],
    *,
    allow_download: bool = True,
    persist: bool = True,
    notify: bool = True,
) -> dict[str, Any]:
    """Validate, apply, persist, and publish one complete Tater settings snapshot."""
    settings = dict(payload or {})
    _normalize_led_settings(settings)
    wake_engine, wake_word_id, available, wake_model = _prepare_wake_word(
        state,
        settings,
        allow_download=allow_download,
    )
    wakeup_sound = _prepare_wake_sound(state, settings, allow_download=allow_download)
    threshold = _bounded_float(settings.get("wake_threshold"), 0.7, 0.01, 0.99)
    volume_percent = _bounded_int(settings.get("volume_percent"), round(state.volume * 100), 0, 100)

    settings["wake_engine"] = wake_engine
    settings["wake_threshold"] = threshold
    settings["volume_percent"] = volume_percent
    state.wakeup_sound = wakeup_sound
    if wake_engine == "micro_wake_word" and available is not None:
        available.probability_cutoff = threshold
        state.available_wake_words[wake_word_id] = available
        state.wake_words = {wake_word_id: wake_model}
        state.active_wake_words = {wake_word_id}
        state.preferences.active_wake_words = [wake_word_id]
        state.preferences.wake_word_1_sensitivity = threshold
        state.wake_word_1_threshold = threshold
    else:
        state.active_wake_words = set()
        state.preferences.active_wake_words = []
    state.wake_words_changed = True

    volume = volume_percent / 100.0
    state.volume = volume
    state.preferences.volume = volume
    state.music_player.set_volume(volume_percent)
    state.tts_player.set_volume(volume_percent)

    state.native_settings = dict(settings)
    state.preferences.native_settings = dict(settings)
    if persist:
        state.save_preferences()

    if notify and state.peripheral_api is not None:
        from .peripheral_api import LVAEvent

        state.peripheral_api.emit_event_sync(LVAEvent.SETTINGS, {"settings": settings})

    _LOGGER.info(
        "Tater settings applied wake_engine=%s wake_word=%s wake_sound=%s volume=%d led=%s/%d",
        wake_engine,
        wake_word_id or "off",
        wakeup_sound or "off",
        volume_percent,
        settings["led_color"],
        settings["led_brightness"],
    )
    return settings
