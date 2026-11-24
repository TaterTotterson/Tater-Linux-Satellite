"""Tater-native controls layered onto the reusable Linux audio state machine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .atomic_file import write_bytes_atomic as _write_bytes_atomic
from .bluetooth_proxy import BluetoothProxy
from .peripheral_api import LVAEvent
from .tater_oww_onnx import OWWWorker, runtime_assets_ready

_LOGGER = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1
_MAX_TIMERS = 8
_MAX_TIMER_SECONDS = 7 * 24 * 60 * 60
_DATA_DIR = Path(os.environ.get("TATER_DATA_DIR") or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tater-satellite")
_SETTINGS_PATH = _DATA_DIR / "tater-live-settings.json"
_OTA_PATH = Path("/data/software.swu")
_SWUPDATE_KEY = Path("/etc/swupdate-public.pem")
_OTA_MAX_BYTES = 192 * 1024 * 1024
_WAKE_MANIFEST_MAX_BYTES = 128 * 1024
_WAKE_MODEL_MAX_BYTES = 2 * 1024 * 1024
_OWW_BUNDLE_MAX_BYTES = 256 * 1024
_OWW_METADATA_MAX_BYTES = 512 * 1024
_OWW_MODEL_MAX_BYTES = 4 * 1024 * 1024
_WAKE_SOUND_MAX_BYTES = 2 * 1024 * 1024
_WAKE_DOWNLOAD_TIMEOUT_SECONDS = 30
_WAKE_URL_SCHEMES = {"http", "https"}
_WAKE_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_OWW_SHARED_DIR = Path(os.environ.get("TATER_WAKE_ASSET_DIR") or Path(__file__).parent / "assets" / "wake")
_OWW_BUILTIN_BUNDLE = _OWW_SHARED_DIR / "hey_tater.wake-bundle.json"
_OWW_BUILTIN_MODEL = _OWW_SHARED_DIR / "hey_tater.oww.onnx"
_NWW_BUILTIN_MODELS = {
    "cnn": _OWW_SHARED_DIR / "hey_tater.nww.cnn.onnx",
    "gru": _OWW_SHARED_DIR / "hey_tater.nww.gru.onnx",
}
_OWW_CACHE_DIR = _DATA_DIR / "openwakeword"
_OWW_AGREEMENT_SECONDS = 1.2
_WAKE_DETECTOR_MODES = {"mww", "oww", "nww", "mww_oww", "mww_nww"}
_NWW_ARCHITECTURES = {"cnn", "gru"}
_TATER_WAKE_SOUND_DIR = Path(__file__).resolve().parent / "assets" / "tater_native"
_WAKE_SOUND_FILES = {
    "blip2": "blip2.wav",
    "message-notification-4": "message-notification-4.wav",
    "notification-ding": "notification-ding.wav",
    "notification-squeak": "notification-squeak.wav",
    "phone-chime": "phone-chime.wav",
    "pop-up-sound": "pop-up-sound.wav",
    "short-definite-fart": "short-definite-fart.wav",
    "star_treck_communications_start_transmission": "star_treck_communications_start_transmission.wav",
    "star_treck_computer_work_beep": "star_treck_computer_work_beep.wav",
    "tater_notify_digital_blip": "tater_notify_digital_blip.wav",
    "turning-off-microphone-percussion-1": "turning-off-microphone-percussion-1.wav",
    "wake_word_triggered": "wake_word_triggered.wav",
    "waterdrop": "waterdrop.wav",
}
_WAKE_SOUND_IDS = {"default", "no_sound", "custom", *_WAKE_SOUND_FILES}
_WAKE_VERIFIER_PACKET = struct.Struct("<4sBBHIII")
_WAKE_VERIFIER_MAX_SAMPLES = 16000 * 2
_WAKE_VERIFIER_MODES = {"off", "observe", "enforce"}
_WAKE_SENSITIVITIES = {"low", "normal", "high", "very_high"}
_WAKE_ENVIRONMENTS = {"far_field", "balanced", "strict", "tv_nearby"}
_MEDIA_SAMPLE_RATE_HZ = 48000
_MEDIA_DEFAULT_OUTPUT_LATENCY_FRAMES = 6144
_AUDIO_PREPARE_TIMEOUT_SECONDS = 10.0
_AUDIO_RAMP_STEP_SECONDS = 0.02
_PLAYBACK_WATCHDOG_INTERVAL_SECONDS = 0.5
_PLAYBACK_STALL_TIMEOUT_SECONDS = 5.0
_SENDSPIN_CHANNELS = {"stereo", "left", "right", "mono"}
_SENDSPIN_PLAYING_MARKER = Path("/tmp/tater-sendspin-playing")
_RETIRED_MEDIA_MESSAGES = {
    "audio.clock.sync",
    "media.session.start",
    "media.session.prepare",
    "media.session.commit",
    "media.session.volume",
    "media.session.adjust",
    "media.session.stop",
}
_LED_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_LED_ANIMATIONS = {"pulse", "breathe", "heartbeat", "solid"}
_LED_SETTING_KEYS = (
    "led_brightness",
    "led_color",
    "led_listening_animation",
    "led_thinking_animation",
    "led_tool_call_animation",
    "led_replying_animation",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _integer(value: Any, default: int = 0, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    try:
        result = int(round(float(value if value is not None else 0)))
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def _normalize_wake_sensitivity(value: Any) -> str:
    normalized = str(value or "normal").strip().lower()
    aliases = {
        "medium": "normal",
        "conservative": "low",
        "sensitive": "high",
        "very_sensitive": "very_high",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _WAKE_SENSITIVITIES else "normal"


def _normalize_wake_environment(value: Any) -> str:
    normalized = str(value or "balanced").strip().lower()
    aliases = {
        "quiet": "far_field",
        "quiet_room": "far_field",
        "very_sensitive": "far_field",
        "tv": "tv_nearby",
        "near_tv": "tv_nearby",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _WAKE_ENVIRONMENTS else "balanced"


def _wake_detection_policy(sensitivity: Any, environment: Any, configured_threshold: Any) -> dict[str, Any]:
    """Return the subset of the shared wake policy supported by microWakeWord."""
    try:
        configured = float(configured_threshold)
    except (TypeError, ValueError):
        configured = 0.97
    configured = max(0.01, min(0.99, configured))
    sensitivity_name = _normalize_wake_sensitivity(sensitivity)
    environment_name = _normalize_wake_environment(environment)

    threshold = configured
    if sensitivity_name == "low":
        threshold = max(threshold, 251.0 / 255.0)
    elif sensitivity_name == "high":
        threshold = min(threshold, 242.0 / 255.0)
    elif sensitivity_name == "very_high":
        threshold = min(threshold, 235.0 / 255.0)

    require_verification = False
    if environment_name == "strict":
        threshold = max(threshold, 247.0 / 255.0)
    elif environment_name == "tv_nearby":
        tv_ceiling = {
            "low": 235.0 / 255.0,
            "normal": 224.0 / 255.0,
            "high": 219.0 / 255.0,
            "very_high": 209.0 / 255.0,
        }[sensitivity_name]
        threshold = min(threshold, tv_ceiling)
        require_verification = True

    return {
        "sensitivity": sensitivity_name,
        "environment": environment_name,
        "configured_threshold": configured,
        "effective_threshold": max(0.01, min(0.99, threshold)),
        "require_verification": require_verification,
    }


def _frame(message_type: str, payload: Optional[dict[str, Any]] = None) -> str:
    return json.dumps(
        {
            "v": _PROTOCOL_VERSION,
            "type": message_type,
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "payload": payload if isinstance(payload, dict) else {},
        },
        separators=(",", ":"),
    )


def _validated_web_url(value: Any, *, label: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _WAKE_URL_SCHEMES or not parsed.netloc:
        raise ValueError(f"{label} must use HTTP or HTTPS")
    return url


def _download_limited(url: str, maximum: int) -> bytes:
    request = Request(url, headers={"User-Agent": "Tater-Linux-Satellite"})
    with urlopen(request, timeout=_WAKE_DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = _validated_web_url(response.geturl(), label="redirected wake-word URL")
        if final_url != response.geturl():
            raise ValueError("invalid redirected wake-word URL")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > maximum:
                    raise ValueError(f"wake-word download exceeds {maximum} bytes")
            except ValueError as exc:
                if "exceeds" in str(exc):
                    raise
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f"wake-word download exceeds {maximum} bytes")
    if not data:
        raise ValueError("wake-word download was empty")
    return bytes(data)


def _custom_wake_id(manifest_url: str) -> str:
    return f"tater_custom_{hashlib.sha256(manifest_url.encode('utf-8')).hexdigest()[:16]}"


def _parse_wake_manifest(manifest_url: str, raw: bytes) -> tuple[dict[str, Any], str, str]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("wake-word manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("wake-word manifest must be a JSON object")
    if manifest.get("type") == "tater_wake_word_bundle":
        manifest = manifest.get("micro_wake_word") or {}
    if str(manifest.get("type") or "").strip().lower() != "micro":
        raise ValueError("Custom wake words must use the microWakeWord format")
    wake_word = str(manifest.get("wake_word") or "").strip()
    if not wake_word or len(wake_word) > 80:
        raise ValueError("wake-word manifest has an invalid wake_word")
    model_ref = str(manifest.get("model") or manifest.get("model_url") or "").strip()
    if not model_ref:
        raise ValueError("wake-word manifest is missing its TFLite model")
    model_url = _validated_web_url(urljoin(manifest_url, model_ref), label="wake-word model URL")
    micro = manifest.get("micro")
    if not isinstance(micro, dict):
        raise ValueError("wake-word manifest is missing its microWakeWord settings")
    return manifest, wake_word, model_url


def _expected_model_sha256(manifest: dict[str, Any]) -> str:
    for key in ("model_sha256", "model_hash", "sha256"):
        value = str(manifest.get(key) or "").strip()
        if value:
            if not _WAKE_SHA256.fullmatch(value):
                raise ValueError(f"wake-word manifest has an invalid {key}")
            return value.lower()
    return ""


def _download_custom_wake_package(state: Any, manifest_url: str) -> tuple[str, Any, Any]:
    from .models import AvailableWakeWord, WakeWordType

    manifest_url = _validated_web_url(manifest_url, label="wake-word manifest URL")
    manifest_raw = _download_limited(manifest_url, _WAKE_MANIFEST_MAX_BYTES)
    manifest, wake_phrase, model_url = _parse_wake_manifest(manifest_url, manifest_raw)
    bundle = json.loads(manifest_raw)
    model_data = _download_limited(model_url, _WAKE_MODEL_MAX_BYTES)
    model_digest = hashlib.sha256(model_data).hexdigest()
    expected_digest = _expected_model_sha256(manifest)
    if expected_digest and model_digest != expected_digest:
        raise ValueError("wake-word model SHA-256 does not match the manifest")

    model_id = _custom_wake_id(manifest_url)
    cache_dir = Path(state.download_dir) / "external_wake_words"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # pymicro-wakeword derives its runtime ID from the model filename, while
    # Linux Voice derives the available/preference ID from the JSON filename.
    # Keep both stems identical so the audio thread can activate the model.
    model_path = cache_dir / f"{model_id}.tflite"
    config_path = cache_dir / f"{model_id}.json"
    cached_manifest = dict(manifest)
    cached_manifest.update(
        {
            "type": "micro",
            "wake_word": wake_phrase,
            "model": model_path.name,
            "_tater_source_url": manifest_url,
            "_tater_model_url": model_url,
            "_tater_model_sha256": model_digest,
        }
    )
    config_data = (json.dumps(cached_manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if bundle.get("type") == "tater_wake_word_bundle":
        from .wake_boundary import cache_boundary_bundle

        cache_boundary_bundle(bundle, manifest_url, cache_dir / f"{model_id}.wake-bundle.json", lambda address: _download_limited(address, _WAKE_MODEL_MAX_BYTES))
    previous_model = model_path.read_bytes() if model_path.is_file() else None
    previous_config = config_path.read_bytes() if config_path.is_file() else None

    try:
        _write_bytes_atomic(model_path, model_data)
        _write_bytes_atomic(config_path, config_data)
        probability_cutoff = max(
            0.01,
            min(0.99, float(manifest.get("micro", {}).get("probability_cutoff", 0.7))),
        )
        available = AvailableWakeWord(
            id=model_id,
            type=WakeWordType.MICRO_WAKE_WORD,
            wake_word=wake_phrase,
            trained_languages=[str(value) for value in manifest.get("trained_languages", []) if str(value).strip()],
            wake_word_path=config_path,
            probability_cutoff=probability_cutoff,
        )
        loaded = available.load()
        if str(getattr(loaded, "id", model_id)) != model_id:
            raise ValueError("wake-word model ID does not match its cached package ID")
    except Exception:
        for path, previous in (
            (model_path, previous_model),
            (config_path, previous_config),
        ):
            if previous is not None:
                _write_bytes_atomic(path, previous)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        raise
    return model_id, available, loaded


@dataclass(frozen=True)
class _OWWPackage:
    classifier_path: Path
    wake_word: str
    threshold: float
    patience: int
    source: str
    engine: str = "open_wake_word"
    architecture: str = ""


def _oww_number(value: Any, *, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"openWakeWord bundle has an invalid {name}") from exc
    if number < 0.01 or number > 1.0:
        raise ValueError(f"openWakeWord bundle has an invalid {name}")
    return number


def _oww_integer(value: Any, *, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"openWakeWord bundle has an invalid {name}") from exc
    if number < 1 or number > 20:
        raise ValueError(f"openWakeWord bundle has an invalid {name}")
    return number


def _bundle_header(bundle: Any, *, engine_name: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(bundle, dict):
        raise ValueError(f"{engine_name} bundle must be a JSON object")
    if str(bundle.get("type") or "").strip() != "tater_wake_word_bundle":
        raise ValueError(f"{engine_name} URL must point to a Tater wake-word bundle")
    wake_word = str(bundle.get("wake_word") or "").strip()
    if not wake_word or len(wake_word) > 80:
        raise ValueError(f"{engine_name} bundle has an invalid wake_word")
    return wake_word, bundle


def _oww_bundle_details(bundle: Any, *, confirmation: bool) -> tuple[str, dict[str, Any], float, int]:
    wake_word, bundle = _bundle_header(bundle, engine_name="openWakeWord")
    section = bundle.get("open_wake_word")
    if not isinstance(section, dict):
        raise ValueError("wake-word bundle is missing open_wake_word")
    artifacts = section.get("artifacts")
    onnx = artifacts.get("onnx") if isinstance(artifacts, dict) else None
    if not isinstance(onnx, dict):
        raise ValueError("wake-word bundle has no ONNX classifier")
    threshold_key = "recommended_confirmation_threshold" if confirmation else "recommended_threshold"
    patience_key = "recommended_confirmation_patience" if confirmation else "recommended_patience"
    threshold = _oww_number(section.get(threshold_key), name=threshold_key)
    patience = _oww_integer(section.get(patience_key), name=patience_key)
    return wake_word, section, threshold, patience


def _nww_bundle_details(
    bundle: Any,
    *,
    architecture: str,
) -> tuple[str, dict[str, Any], dict[str, Any], float, int]:
    wake_word, bundle = _bundle_header(bundle, engine_name="NanoWakeWord")
    architecture = str(architecture or "").strip().lower()
    if architecture not in _NWW_ARCHITECTURES:
        raise ValueError(f"unsupported NanoWakeWord architecture: {architecture or 'empty'}")
    section = bundle.get("nano_wake_word")
    variants = section.get("variants") if isinstance(section, dict) else None
    variant = variants.get(architecture) if isinstance(variants, dict) else None
    if not isinstance(variant, dict):
        raise ValueError(f"wake-word bundle has no NanoWakeWord {architecture.upper()} model")
    model_ref = str(variant.get("model") or "").strip()
    artifact = {
        "file": model_ref,
        "sha256": variant.get("model_sha256"),
        "size_bytes": variant.get("model_size_bytes"),
    }
    if not model_ref:
        artifacts = variant.get("artifacts")
        candidate = artifacts.get("onnx") if isinstance(artifacts, dict) else None
        if isinstance(candidate, dict):
            artifact = candidate
            model_ref = str(artifact.get("file") or "").strip()
    if not model_ref.lower().endswith(".onnx") or Path(model_ref).name != model_ref:
        raise ValueError(f"NanoWakeWord {architecture.upper()} model reference is invalid")
    threshold = _oww_number(variant.get("recommended_threshold"), name="recommended_threshold")
    patience = _oww_integer(variant.get("recommended_patience"), name="recommended_patience")
    return wake_word, variant, artifact, threshold, patience


def _verified_sha256(data: bytes, expected: Any, *, label: str) -> str:
    digest = str(expected or "").strip().lower()
    if not _WAKE_SHA256.fullmatch(digest):
        raise ValueError(f"openWakeWord bundle has an invalid {label} SHA-256")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"openWakeWord {label} SHA-256 does not match the bundle")
    return digest


def _load_builtin_oww_package(*, confirmation: bool) -> _OWWPackage:
    if not runtime_assets_ready():
        raise ValueError("the ONNX openWakeWord runtime is not installed")
    try:
        raw = _OWW_BUILTIN_BUNDLE.read_bytes()
        bundle = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the built-in openWakeWord bundle is unreadable") from exc
    wake_word, section, threshold, patience = _oww_bundle_details(
        bundle,
        confirmation=confirmation,
    )
    artifacts = section["artifacts"]
    onnx = artifacts["onnx"]
    model_data = _OWW_BUILTIN_MODEL.read_bytes()
    _verified_sha256(model_data, onnx.get("sha256"), label="model")
    if len(model_data) != _integer(onnx.get("size_bytes"), maximum=_OWW_MODEL_MAX_BYTES):
        raise ValueError("built-in openWakeWord model size does not match the bundle")
    return _OWWPackage(
        classifier_path=_OWW_BUILTIN_MODEL,
        wake_word=wake_word,
        threshold=threshold,
        patience=patience,
        source="embedded",
    )


def _download_oww_package(bundle_url: str, *, confirmation: bool) -> _OWWPackage:
    if not runtime_assets_ready():
        raise ValueError("the ONNX openWakeWord runtime is not installed")
    bundle_url = _validated_web_url(bundle_url, label="openWakeWord bundle URL")
    bundle_raw = _download_limited(bundle_url, _OWW_BUNDLE_MAX_BYTES)
    try:
        bundle = json.loads(bundle_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("openWakeWord bundle is not valid UTF-8 JSON") from exc
    wake_word, section, threshold, patience = _oww_bundle_details(
        bundle,
        confirmation=confirmation,
    )
    metadata_ref = str(section.get("metadata") or "").strip()
    onnx = section["artifacts"]["onnx"]
    model_ref = str(onnx.get("file") or "").strip()
    if not model_ref:
        raise ValueError("openWakeWord bundle has incomplete artifact references")
    model_url = _validated_web_url(urljoin(bundle_url, model_ref), label="openWakeWord model URL")
    metadata = b""
    if metadata_ref:
        metadata_url = _validated_web_url(urljoin(bundle_url, metadata_ref), label="openWakeWord metadata URL")
        metadata = _download_limited(metadata_url, _OWW_METADATA_MAX_BYTES)
        _verified_sha256(metadata, section.get("metadata_sha256"), label="metadata")
    model = _download_limited(model_url, _OWW_MODEL_MAX_BYTES)
    model_digest = _verified_sha256(model, onnx.get("sha256"), label="model")
    expected_size = _integer(onnx.get("size_bytes"), maximum=_OWW_MODEL_MAX_BYTES)
    if expected_size and len(model) != expected_size:
        raise ValueError("openWakeWord model size does not match the bundle")

    _OWW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prefix = hashlib.sha256(bundle_url.encode("utf-8")).hexdigest()[:16]
    model_path = _OWW_CACHE_DIR / f"{prefix}.onnx"
    metadata_path = _OWW_CACHE_DIR / f"{prefix}.json"
    bundle_path = _OWW_CACHE_DIR / f"{prefix}.wake-bundle.json"
    from .wake_boundary import cache_boundary_bundle

    cache_boundary_bundle(bundle, bundle_url, bundle_path, lambda address: _download_limited(address, _WAKE_MODEL_MAX_BYTES))
    cached_bundle = json.loads(bundle_path.read_text()) if bundle.get("wake_boundary") else dict(bundle)
    cached_bundle["_tater_source_url"] = bundle_url
    cached_bundle["_tater_model_sha256"] = model_digest
    _write_bytes_atomic(model_path, model)
    if metadata:
        _write_bytes_atomic(metadata_path, metadata)
    else:
        try:
            metadata_path.unlink()
        except FileNotFoundError:
            pass
    _write_bytes_atomic(
        bundle_path,
        (json.dumps(cached_bundle, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return _OWWPackage(
        classifier_path=model_path,
        wake_word=wake_word,
        threshold=threshold,
        patience=patience,
        source=bundle_url,
    )


def _load_builtin_nww_package(architecture: str) -> _OWWPackage:
    if not runtime_assets_ready():
        raise ValueError("the ONNX NanoWakeWord runtime is not installed")
    architecture = str(architecture or "").strip().lower()
    model_path = _NWW_BUILTIN_MODELS.get(architecture)
    if model_path is None:
        raise ValueError(f"unsupported NanoWakeWord architecture: {architecture or 'empty'}")
    try:
        bundle = json.loads(_OWW_BUILTIN_BUNDLE.read_text(encoding="utf-8"))
        model_data = model_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the built-in NanoWakeWord package is unreadable") from exc
    wake_word, _variant, artifact, threshold, patience = _nww_bundle_details(
        bundle,
        architecture=architecture,
    )
    _verified_sha256(model_data, artifact.get("sha256"), label="model")
    expected_size = _integer(artifact.get("size_bytes"), maximum=_OWW_MODEL_MAX_BYTES)
    if not expected_size or len(model_data) != expected_size:
        raise ValueError("built-in NanoWakeWord model size does not match the bundle")
    return _OWWPackage(
        classifier_path=model_path,
        wake_word=wake_word,
        threshold=threshold,
        patience=patience,
        source="embedded",
        engine="nano_wake_word",
        architecture=architecture,
    )


def _download_nww_package(bundle_url: str, *, architecture: str) -> _OWWPackage:
    if not runtime_assets_ready():
        raise ValueError("the ONNX NanoWakeWord runtime is not installed")
    bundle_url = _validated_web_url(bundle_url, label="NanoWakeWord bundle URL")
    bundle_raw = _download_limited(bundle_url, _OWW_BUNDLE_MAX_BYTES)
    try:
        bundle = json.loads(bundle_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("NanoWakeWord bundle is not valid UTF-8 JSON") from exc
    wake_word, _variant, artifact, threshold, patience = _nww_bundle_details(
        bundle,
        architecture=architecture,
    )
    model_ref = str(artifact.get("file") or "").strip()
    model_url = _validated_web_url(urljoin(bundle_url, model_ref), label="NanoWakeWord model URL")
    model = _download_limited(model_url, _OWW_MODEL_MAX_BYTES)
    model_digest = _verified_sha256(model, artifact.get("sha256"), label="model")
    expected_size = _integer(artifact.get("size_bytes"), maximum=_OWW_MODEL_MAX_BYTES)
    if not expected_size or len(model) != expected_size:
        raise ValueError("NanoWakeWord model size does not match the bundle")

    _OWW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prefix = hashlib.sha256(f"{bundle_url}\n{architecture}".encode("utf-8")).hexdigest()[:16]
    model_path = _OWW_CACHE_DIR / f"{prefix}.nww.{architecture}.onnx"
    bundle_path = _OWW_CACHE_DIR / f"{prefix}.wake-bundle.json"
    from .wake_boundary import cache_boundary_bundle

    cache_boundary_bundle(bundle, bundle_url, bundle_path, lambda address: _download_limited(address, _WAKE_MODEL_MAX_BYTES))
    cached_bundle = json.loads(bundle_path.read_text()) if bundle.get("wake_boundary") else dict(bundle)
    cached_bundle["_tater_source_url"] = bundle_url
    cached_bundle["_tater_model_sha256"] = model_digest
    _write_bytes_atomic(model_path, model)
    _write_bytes_atomic(
        bundle_path,
        (json.dumps(cached_bundle, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return _OWWPackage(
        classifier_path=model_path,
        wake_word=wake_word,
        threshold=threshold,
        patience=patience,
        source=bundle_url,
        engine="nano_wake_word",
        architecture=architecture,
    )


def _wake_sound_cache_path() -> Path:
    return _SETTINGS_PATH.with_name("tater-wake-sound.wav")


def _wake_sound_cache_metadata_path() -> Path:
    return _SETTINGS_PATH.with_name("tater-wake-sound.json")


def _validate_wav(data: bytes) -> None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("custom wake sound is not a RIFF/WAVE file")


def _cached_custom_wake_sound(url: str) -> Optional[Path]:
    sound_path = _wake_sound_cache_path()
    metadata_path = _wake_sound_cache_metadata_path()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or str(metadata.get("url") or "") != url:
            return None
        data = sound_path.read_bytes()
        _validate_wav(data)
        expected_sha256 = str(metadata.get("sha256") or "").strip().lower()
        if not _WAKE_SHA256.fullmatch(expected_sha256):
            return None
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            return None
        return sound_path
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _download_custom_wake_sound(url: str) -> Path:
    url = _validated_web_url(url, label="wake-sound URL")
    data = _download_limited(url, _WAKE_SOUND_MAX_BYTES)
    _validate_wav(data)
    sound_path = _wake_sound_cache_path()
    metadata_path = _wake_sound_cache_metadata_path()
    sound_path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(sound_path, data)
    metadata = {
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }
    _write_bytes_atomic(
        metadata_path,
        (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return sound_path


@dataclass
class _Timer:
    timer_id: str
    name: str
    original_duration_ms: int
    deadline: float
    task: Optional[asyncio.Task[None]] = None
    ringing: bool = False

    def public(self) -> dict[str, Any]:
        remaining_ms = 0 if self.ringing else max(0, int(round((self.deadline - time.monotonic()) * 1000)))
        return {
            "id": self.timer_id,
            "name": self.name,
            "label": self.name,
            "state": "ringing" if self.ringing else "armed",
            "active": True,
            "ringing": self.ringing,
            "original_duration_ms": self.original_duration_ms,
            "duration_ms": self.original_duration_ms,
            "remaining_ms": remaining_ms,
        }


@dataclass
class _OverlaySession:
    overlay_id: str
    url: str
    group_id: str = ""
    start_at_us: int = 0
    volume_percent: int = 100
    duck_target: float = 0.2
    attack_ms: int = 150
    release_ms: int = 350
    continue_conversation: bool = False
    generation: int = 0
    task: Optional[asyncio.Task[None]] = None
    started: bool = False
    finished: bool = False


@dataclass
class _AudioScene:
    scene_id: str
    foreground_url: str
    background_url: str = ""
    foreground_volume_percent: int = 100
    background_volume_percent: int = 60
    background_loop: bool = True
    duck_target: float = 0.2
    attack_ms: int = 150
    release_ms: int = 350
    fade_ms: int = 350
    generation: int = 0
    task: Optional[asyncio.Task[None]] = None
    started: bool = False
    finished: bool = False


class _PlaybackStalledError(RuntimeError):
    """Raised when an active MPV output stops advancing."""


class TaterFeatureManager:
    """Handle Tater commands that are independent of the voice transport."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.satellite = client.satellite
        self.state: Any = client.state
        self.timers: dict[str, _Timer] = {}
        self.overlay_session: Optional[_OverlaySession] = None
        self.audio_scene: Optional[_AudioScene] = None
        self._overlay_generation = 0
        self._scene_generation = 0
        self._music_duck_factor = 1.0
        self._sync_player_available = bool(
            getattr(self._player("music_player"), "synchronized_controls_available", False)
            and all(
                callable(getattr(self._player("music_player"), method, None))
                for method in (
                    "prepare_synchronized",
                    "synchronized_snapshot",
                    "seek_synchronized",
                    "jump_synchronized",
                    "set_synchronized_speed",
                    "reset_synchronized",
                    "resume_synchronized",
                )
            )
        )
        self._sync_overlay_available = bool(
            self._sync_player_available
            and getattr(self._player("tts_player"), "synchronized_controls_available", False)
            and all(
                callable(getattr(self._player("tts_player"), method, None))
                for method in (
                    "prepare_synchronized",
                    "synchronized_snapshot",
                    "reset_synchronized",
                    "resume",
                )
            )
        )
        self.ota_task: Optional[asyncio.Task[None]] = None
        self.wake_model_task: Optional[asyncio.Task[None]] = None
        self.wake_model_generation = 0
        self.wake_model_downloading = False
        self.oww_model_task: Optional[asyncio.Task[None]] = None
        self.oww_model_generation = 0
        self.oww_model_downloading = False
        self._oww_lock = threading.Lock()
        self._oww_worker: Optional[OWWWorker] = None
        self._oww_model_name = ""
        self._oww_model_source = ""
        self._oww_engine_name = ""
        self._oww_architecture = ""
        self._oww_last_error = ""
        self._oww_mww_candidate = 0.0
        self._oww_candidate = 0.0
        self._oww_agreements = 0
        self.wake_sound_task: Optional[asyncio.Task[None]] = None
        self.wake_sound_generation = 0
        self.wake_sound_downloading = False
        self.wake_verifier_timeout_task: Optional[asyncio.Task[None]] = None
        self._wake_audio_lock = threading.Lock()
        self._wake_audio = bytearray()
        self._wake_verifier_lock = threading.Lock()
        self._wake_verifier_generation = 0
        self._wake_verifier_last_id = 0
        self._wake_verifier_completed_id = 0
        self._wake_verifier_pending_id = 0
        self._wake_verifier_pending_word = ""
        self._wake_verifier_count = 0
        self._wake_verifier_rejections = 0
        self._wake_verifier_fail_open = 0
        self._wake_verifier_last_reason = ""
        self._default_wakeup_sound = str(getattr(self.state, "wakeup_sound", "") or "")
        self.settings = self._load_settings()
        self.state.native_settings = self.settings
        preferences = getattr(self.state, "preferences", None)
        preferred = getattr(preferences, "wake_word_1_sensitivity", None)
        active: Any = getattr(preferences, "active_wake_words", None) or getattr(self.state, "active_wake_words", ())
        model_id = next(iter(active), "")
        available = getattr(self.state, "available_wake_words", {}).get(model_id)
        default_threshold = preferred if preferred is not None else getattr(available, "probability_cutoff", getattr(self.state, "wake_word_1_threshold", 0.97))
        try:
            self._configured_wake_threshold = float(
                self.settings.get(
                    "wake_threshold",
                    default_threshold,
                )
            )
        except (TypeError, ValueError):
            self._configured_wake_threshold = 0.97
        self._configured_wake_threshold = max(
            0.01,
            min(0.99, self._configured_wake_threshold),
        )
        self.satellite._tater_barge_in_enabled = bool(getattr(self.state, "tater_playback_reference_aec", False)) and _truthy(self.settings.get("barge_in_enabled"))  # pylint: disable=protected-access
        self.satellite._tater_wakeup_sound = self._default_wakeup_sound  # pylint: disable=protected-access
        self.satellite._tater_features = self  # pylint: disable=protected-access
        self.capabilities: dict[str, Any] = {
            "live_settings": True,
            "custom_wake_words": True,
            "openwakeword": runtime_assets_ready(),
            "nanowakeword": runtime_assets_ready(),
            "nanowakeword_architectures": ["cnn", "gru"] if runtime_assets_ready() else [],
            "wake_model_tuning_overrides": True,
            "wake_detector_selection": True,
            "dual_wake_confirmation": True,
            "dual_wake_agreement_ms": int(_OWW_AGREEMENT_SECONDS * 1000),
            "wake_sounds": True,
            "custom_wake_sounds": True,
            "wake_verifier": True,
            "wake_sensitivity": True,
            "wake_environment_profiles": True,
            "wake_during_playback": bool(getattr(self.state, "tater_playback_reference_aec", False)),
            "playback_reference_aec": bool(getattr(self.state, "tater_playback_reference_aec", False)),
            "status_led": False,
            "setup_mode": False,
            "timers": True,
            "ota": False,
            "audio_ducking": True,
            "tts_overlays": True,
            "audio_scenes": self._sync_overlay_available,
            "looping_background_audio": self._sync_overlay_available,
            "barge_in": bool(getattr(self.state, "tater_playback_reference_aec", False)),
            "audio_stall_recovery": self._sync_overlay_available,
            "audio_scene_version": 1 if self._sync_overlay_available else 0,
            "sendspin_player": bool(getattr(client, "sendspin", None) and client.sendspin.available),
            "sendspin_version": 1 if getattr(client, "sendspin", None) and client.sendspin.available else 0,
            "sendspin_output_channel_selection": bool(getattr(client, "sendspin", None) and client.sendspin.channel_selection),
            "sendspin_output_channel_modes": ["stereo", "left", "right", "mono"] if getattr(client, "sendspin", None) and client.sendspin.channel_selection else ["stereo"],
            "ble_advertisements": True,
            "ble_advertisements_version": 1,
        }
        self.bluetooth = BluetoothProxy(self._send, should_pause=self._ble_should_pause)
        self.capabilities.update(self.bluetooth.capabilities())

    def _send(self, message_type: str, payload: Optional[dict[str, Any]] = None) -> None:
        self.client._submit_frame(_frame(message_type, payload))  # pylint: disable=protected-access

    def _call_soon(self, callback: Any, *args: Any) -> None:
        """Run player callbacks on the asyncio loop that owns this manager."""
        loop = getattr(self.client, "_loop", None)
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(callback, *args)
            return
        try:
            asyncio.get_running_loop().call_soon(callback, *args)
        except RuntimeError:
            callback(*args)

    def _wake_policy(self) -> dict[str, Any]:
        return _wake_detection_policy(
            self.settings.get("wake_sensitivity"),
            self.settings.get("wake_environment"),
            self._configured_wake_threshold,
        )

    def _wake_detector_mode(self) -> str:
        mode = str(self.settings.get("wake_detector_mode") or "").strip().lower()
        if mode == "dual":
            return "mww_oww"
        if mode in _WAKE_DETECTOR_MODES:
            return mode
        mww_enabled = _truthy(self.settings.get("wake_mww_enabled", True))
        oww_enabled = _truthy(self.settings.get("wake_oww_enabled", False))
        nww_enabled = _truthy(self.settings.get("wake_nww_enabled", False))
        if mww_enabled and nww_enabled:
            return "mww_nww"
        if mww_enabled and oww_enabled:
            return "mww_oww"
        if nww_enabled:
            return "nww"
        if oww_enabled:
            return "oww"
        return "mww"

    def wake_mww_enabled(self) -> bool:
        return self._wake_detector_mode() in {"mww", "mww_oww", "mww_nww"}

    def _companion_engine(self) -> str:
        mode = self._wake_detector_mode()
        if mode in {"nww", "mww_nww"}:
            return "nano_wake_word"
        if mode in {"oww", "mww_oww"}:
            return "open_wake_word"
        return ""

    def boundary_bundle(self, phrase: str):
        bundle = _OWW_SHARED_DIR / "hey_tater.wake-bundle.json" if phrase == "hey tater" else None
        settings = self.settings
        companion = self._companion_engine()
        if companion:
            key = "nww" if companion == "nano_wake_word" else "oww"
            url = str(settings.get(key + "_wake_word_url") or "")
            if settings.get(key + "_wake_word") in {"custom_url", "paired_bundle"} and url:
                token = url + ("\n" + str(settings.get("nww_architecture") or "cnn") if key == "nww" else "")
                bundle = _OWW_CACHE_DIR / (hashlib.sha256(token.encode()).hexdigest()[:16] + ".wake-bundle.json")
        else:
            active: Any = getattr(self.state, "active_wake_words", set())
            available = getattr(self.state, "available_wake_words", {})
            for model_id in active:
                model = available.get(model_id)
                path = getattr(model, "wake_word_path", None)
                if path and str(model_id).startswith("tater_custom_"):
                    bundle = Path(path).with_name(str(model_id) + ".wake-bundle.json")
                    break
        return bundle

    def submit_wake_audio(self, pcm: bytes) -> None:
        if not self._companion_engine():
            return
        with self._oww_lock:
            worker = self._oww_worker
        if worker is not None:
            worker.submit(pcm)

    def resolve_wake_detection(self, mww_detected: bool) -> bool:
        """Combine MWW with the selected ONNX wake lane without blocking capture."""
        mode = self._wake_detector_mode()
        now = time.monotonic()
        with self._oww_lock:
            worker = self._oww_worker
        oww_event = worker.pop_detection() if worker is not None else None
        if str(self.settings.get("wake_engine") or "micro_wake_word") in {"off", "button"}:
            return False
        if mode == "mww":
            return bool(mww_detected)
        if mode in {"oww", "nww"}:
            return oww_event is not None
        if mode not in {"mww_oww", "mww_nww"}:
            return False
        if mww_detected:
            self._oww_mww_candidate = now
        if oww_event is not None:
            self._oww_candidate = float(oww_event[0])
        cutoff = now - _OWW_AGREEMENT_SECONDS
        if self._oww_mww_candidate < cutoff:
            self._oww_mww_candidate = 0.0
        if self._oww_candidate < cutoff:
            self._oww_candidate = 0.0
        if self._oww_mww_candidate > 0.0 and self._oww_candidate > 0.0 and abs(self._oww_mww_candidate - self._oww_candidate) <= _OWW_AGREEMENT_SECONDS:
            self._oww_mww_candidate = 0.0
            self._oww_candidate = 0.0
            self._oww_agreements += 1
            return True
        return False

    def _oww_status(self) -> dict[str, Any]:
        with self._oww_lock:
            worker = self._oww_worker
        worker_status = worker.status() if worker is not None else {"ready": False}
        return {
            **worker_status,
            "enabled": bool(self._companion_engine()),
            "downloading": self.oww_model_downloading,
            "model": self._oww_model_name,
            "source": self._oww_model_source,
            "engine": self._oww_engine_name or self._companion_engine(),
            "architecture": self._oww_architecture,
            "agreement_window_ms": int(_OWW_AGREEMENT_SECONDS * 1000),
            "agreements": self._oww_agreements,
            "last_error": self._oww_last_error or str(worker_status.get("last_error") or ""),
        }

    def _wake_verification_required(self) -> bool:
        return bool(self._wake_policy()["require_verification"])

    def _apply_wake_policy(self, *, persist_preferences: bool) -> None:
        policy = self._wake_policy()
        threshold = float(policy["effective_threshold"])
        changed = (
            abs(float(getattr(self.state, "wake_word_1_threshold", threshold)) - threshold) > 0.0001 or getattr(getattr(self.state, "preferences", None), "wake_word_1_sensitivity", None) != threshold
        )
        self.state.wake_word_1_threshold = threshold
        if getattr(self.state, "preferences", None) is not None:
            self.state.preferences.wake_word_1_sensitivity = threshold
        if changed and persist_preferences:
            self.state.save_preferences()

    def capture_wake_verifier_audio(self, pcm: bytes) -> None:
        """Keep the latest two seconds of primary-microphone PCM16 audio."""
        mode = str(self.settings.get("wake_verifier_mode") or "off").strip().lower()
        if mode not in {"observe", "enforce"} and not self._wake_verification_required():
            return
        raw = bytes(pcm or b"")
        if len(raw) < 2:
            return
        if len(raw) % 2:
            raw = raw[:-1]
        maximum_bytes = _WAKE_VERIFIER_MAX_SAMPLES * 2
        with self._wake_audio_lock:
            self._wake_audio.extend(raw)
            overflow = len(self._wake_audio) - maximum_bytes
            if overflow > 0:
                del self._wake_audio[:overflow]

    def _wake_verifier_audio_tail(self, window_ms: int) -> bytes:
        requested_samples = _integer(
            (16000 * window_ms) / 1000,
            16000,
            minimum=1,
            maximum=_WAKE_VERIFIER_MAX_SAMPLES,
        )
        requested_bytes = requested_samples * 2
        with self._wake_audio_lock:
            return bytes(self._wake_audio[-requested_bytes:])

    def _wake_verifier_status(self) -> dict[str, Any]:
        with self._wake_verifier_lock:
            return {
                "pending": bool(self._wake_verifier_pending_id),
                "pending_id": self._wake_verifier_pending_id,
                "completed": self._wake_verifier_count,
                "rejections": self._wake_verifier_rejections,
                "fail_open": self._wake_verifier_fail_open,
                "last_reason": self._wake_verifier_last_reason,
            }

    def begin_wake_verification(self, wake_word: str) -> bool:
        """Upload a TWV1 wake clip; return True while enforcement defers STT."""
        mode = str(self.settings.get("wake_verifier_mode") or "off").strip().lower()
        force_enforce = self._wake_verification_required()
        if mode not in {"observe", "enforce"} and not force_enforce:
            return False
        enforce = force_enforce or mode == "enforce"
        with self._wake_verifier_lock:
            if self._wake_verifier_pending_id:
                return True

        loop = getattr(self.client, "_loop", None)
        connected = bool(getattr(self.client, "connected", False))
        if loop is None or loop.is_closed() or not connected:
            return False
        window_ms = _integer(
            self.settings.get("wake_verifier_window_ms"),
            1000,
            minimum=500,
            maximum=2000,
        )
        pcm = self._wake_verifier_audio_tail(window_ms)
        sample_count = len(pcm) // 2
        if sample_count < 1:
            self._send("log", {"level": "warn", "message": "Wake verifier skipped because its microphone buffer is empty."})
            return False

        with self._wake_verifier_lock:
            self._wake_verifier_generation = (self._wake_verifier_generation + 1) & 0xFFFFFFFF
            if self._wake_verifier_generation == 0:
                self._wake_verifier_generation = 1
            request_id = self._wake_verifier_generation
            self._wake_verifier_last_id = request_id
            self._wake_verifier_completed_id = 0
            self._wake_verifier_last_reason = "pending"
            if enforce:
                self._wake_verifier_pending_id = request_id
                self._wake_verifier_pending_word = str(wake_word or "")[:120]

        flags = 0x01 if enforce else 0
        packet = (
            _WAKE_VERIFIER_PACKET.pack(
                b"TWV1",
                1,
                1,
                flags,
                request_id,
                16000,
                sample_count,
            )
            + pcm
        )
        try:
            self.client._submit_frame(packet)  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to upload a Tater wake-verifier clip")
            if enforce:
                self._complete_wake_verification(
                    request_id,
                    accepted=True,
                    available=False,
                    reason="upload_fail_open",
                )
            return False
        if enforce:
            timeout_ms = _integer(
                self.settings.get("wake_verifier_timeout_ms"),
                500,
                minimum=100,
                maximum=2000,
            )
            loop.call_soon_threadsafe(
                self._arm_wake_verifier_timeout,
                request_id,
                timeout_ms,
            )
        return enforce

    def _arm_wake_verifier_timeout(self, request_id: int, timeout_ms: int) -> None:
        with self._wake_verifier_lock:
            active = request_id == self._wake_verifier_pending_id and request_id != self._wake_verifier_completed_id
        if not active:
            return
        if self.wake_verifier_timeout_task is not None and not self.wake_verifier_timeout_task.done():
            self.wake_verifier_timeout_task.cancel()
        self.wake_verifier_timeout_task = asyncio.create_task(self._wake_verifier_timeout(request_id, timeout_ms))

    async def _wake_verifier_timeout(self, request_id: int, timeout_ms: int) -> None:
        try:
            await asyncio.sleep(timeout_ms / 1000.0)
            self._complete_wake_verification(
                request_id,
                accepted=True,
                available=False,
                reason="satellite_timeout_fail_open",
            )
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise

    def _complete_wake_verification(
        self,
        request_id: int,
        *,
        accepted: bool,
        available: bool,
        reason: str,
    ) -> None:
        wake_word = ""
        enforced = False
        should_start = False
        with self._wake_verifier_lock:
            if request_id == 0 or request_id != self._wake_verifier_last_id or request_id == self._wake_verifier_completed_id:
                return
            self._wake_verifier_completed_id = request_id
            self._wake_verifier_count += 1
            fail_open = not available
            if (not accepted) and available:
                self._wake_verifier_rejections += 1
            if fail_open:
                self._wake_verifier_fail_open += 1
            self._wake_verifier_last_reason = str(reason or ("accepted" if accepted else "rejected"))[:120]
            if request_id == self._wake_verifier_pending_id:
                enforced = True
                should_start = accepted or fail_open
                wake_word = self._wake_verifier_pending_word
                self._wake_verifier_pending_id = 0
                self._wake_verifier_pending_word = ""

        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        timeout_task = self.wake_verifier_timeout_task
        if timeout_task is not None and timeout_task is not current and not timeout_task.done():
            timeout_task.cancel()
        if timeout_task is not current:
            self.wake_verifier_timeout_task = None
        if not enforced:
            return
        if should_start:
            self._send("log", {"level": "info", "message": "Wake verifier accepted the wake." if available else "Wake verifier timed out; continuing fail-open."})
            start_verified = getattr(self.satellite, "_tater_start_verified_wake", None)
            if callable(start_verified):
                start_verified(wake_word)
        else:
            self._send("log", {"level": "info", "message": "Wake verifier rejected a false wake."})

    def _fail_open_pending_wake_verification(self, reason: str) -> None:
        with self._wake_verifier_lock:
            request_id = self._wake_verifier_pending_id
        if request_id:
            self._complete_wake_verification(
                request_id,
                accepted=True,
                available=False,
                reason=reason,
            )

    async def _sleep_until_us(self, deadline_us: int) -> None:
        while deadline_us > 0:
            remaining_us = deadline_us - (time.monotonic_ns() // 1000)
            if remaining_us <= 0:
                return
            if remaining_us > 20_000:
                await asyncio.sleep((remaining_us - 10_000) / 1_000_000.0)
            elif remaining_us > 1_000:
                await asyncio.sleep(0.001)
            else:
                await asyncio.sleep(0)

    async def _wait_player_ready(self, player: Any, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_snapshot = player.synchronized_snapshot()
            if _truthy(last_snapshot.get("loaded")) and not _truthy(last_snapshot.get("seeking")):
                return last_snapshot
            await asyncio.sleep(0.02)
        raise TimeoutError("audio preparation timed out")

    @staticmethod
    def _player_rendered_position(snapshot: dict[str, Any]) -> Optional[float]:
        value = snapshot.get("rendered_position_seconds")
        if value is None:
            value = snapshot.get("position_seconds")
        if value is None:
            value = snapshot.get("timeline_position_seconds")
        try:
            return max(0.0, float(value if value is not None else 0))
        except (TypeError, ValueError):
            return None

    async def _wait_for_playback_completion(
        self,
        finished: asyncio.Future[None],
        players: tuple[tuple[str, Any], ...],
        is_active: Callable[[], bool],
    ) -> bool:
        """Wait for completion while ensuring every active MPV clock advances."""
        last_positions: dict[int, Optional[float]] = {}
        last_progress: dict[int, float] = {}
        started_at = time.monotonic()
        for _label, player in players:
            player_id = id(player)
            snapshot = player.synchronized_snapshot()
            last_positions[player_id] = self._player_rendered_position(snapshot)
            last_progress[player_id] = started_at

        while is_active():
            done, _pending = await asyncio.wait(
                (finished,),
                timeout=_PLAYBACK_WATCHDOG_INTERVAL_SECONDS,
            )
            if done:
                await finished
                return True

            now = time.monotonic()
            for label, player in players:
                snapshot = player.synchronized_snapshot()
                position = self._player_rendered_position(snapshot)
                player_id = id(player)
                previous = last_positions[player_id]
                if position is not None and (previous is None or position != previous):
                    last_positions[player_id] = position
                    last_progress[player_id] = now
                    continue
                if now - last_progress[player_id] >= _PLAYBACK_STALL_TIMEOUT_SECONDS:
                    state = "buffering" if _truthy(snapshot.get("rebuffering")) else "rendering"
                    raise _PlaybackStalledError(f"{label} remained stalled while {state} for " f"{_PLAYBACK_STALL_TIMEOUT_SECONDS:g} seconds")
        return False

    def _player(self, name: str) -> Any:
        """Return a transport-provided optional audio player."""
        return getattr(self.state, name, None)

    def _set_music_duck(self, factor: float) -> None:
        self._music_duck_factor = max(0.0, min(1.0, float(factor)))
        sendspin = getattr(self.client, "sendspin", None)
        if sendspin is not None:
            sendspin.duck(self._music_duck_factor)
        player = self._player("music_player")
        if player is not None:
            if self._music_duck_factor >= 0.999:
                player.unduck()
            else:
                player.duck(self._music_duck_factor)

    async def _ramp_music_duck(self, target: float, duration_ms: int) -> None:
        target = max(0.0, min(1.0, float(target)))
        duration_seconds = max(0.0, duration_ms / 1000.0)
        start = self._music_duck_factor
        if duration_seconds <= 0.0 or abs(target - start) < 0.001:
            self._set_music_duck(target)
            return
        steps = max(1, int(round(duration_seconds / _AUDIO_RAMP_STEP_SECONDS)))
        step_delay = duration_seconds / steps
        for index in range(1, steps + 1):
            self._set_music_duck(start + ((target - start) * index / steps))
            await asyncio.sleep(step_delay)

    def _load_settings(self) -> dict[str, Any]:
        if getattr(self.client, "uses_shared_features", False):
            return dict(getattr(self.state, "native_settings", {}) or {})
        try:
            value = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_settings(self) -> None:
        if getattr(self.client, "sendspin", None) is not None:
            self.client.sendspin.output_channel_mode = str(self.settings.get("output_channel_mode") or "stereo")
        if getattr(self.client, "uses_shared_features", False):
            self.state.native_settings = dict(self.settings)
            if getattr(self.state, "preferences", None) is not None:
                self.state.preferences.native_settings = dict(self.settings)
                self.state.save_preferences()
            return
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = _SETTINGS_PATH.with_name(f".{_SETTINGS_PATH.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, _SETTINGS_PATH)
        except OSError:
            _LOGGER.exception("Unable to persist Tater live settings")

    def _restart_sendspin(self) -> None:
        player = getattr(self.client, "sendspin", None)
        if player is not None:
            player.restart()

    async def _reset_setup(self) -> None:
        self._send("log", {"level": "error", "message": "Setup reset is not supported by this runtime."})

    async def _run_ota(self, payload: dict[str, Any]) -> None:
        self._send("ota.status", {"status": "error", "message": "Use the operating system to update this satellite."})

    def connected(self) -> None:
        if getattr(self.client, "uses_shared_features", False):
            self.apply_advanced_settings(self.settings, persist=False)
        else:
            self._apply_settings(self.settings, persist=False)

    def apply_advanced_settings(self, payload, *, persist=True):
        keys = {
            "wake_sensitivity",
            "wake_environment",
            "wake_detector_mode",
            "wake_mww_enabled",
            "wake_oww_enabled",
            "wake_nww_enabled",
            "oww_wake_word",
            "oww_wake_word_url",
            "nww_wake_word",
            "nww_wake_word_url",
            "nww_model_revision",
            "nww_architecture",
            "oww_threshold",
            "oww_patience",
            "nww_threshold",
            "nww_patience",
            "barge_in_enabled",
            "wake_threshold",
            "output_channel_mode",
        }
        advanced = {key: value for key, value in payload.items() if key in keys}
        if advanced or not persist:
            self._apply_settings(advanced, persist=persist)
        return {key: value for key, value in self.settings.items() if key in keys}

    def disconnected(self) -> None:
        # Timers and Sendspin playback are independent of the Tater control
        # connection, so both intentionally survive a server reconnect.
        self._fail_open_pending_wake_verification("disconnect_fail_open")
        if self.audio_scene is not None:
            self._stop_audio_scene(ok=False, notify=False)
        if self.overlay_session is not None:
            self._stop_overlay(ok=False, notify=False)
        self._call_soon(lambda: asyncio.create_task(self.bluetooth.close()))

    def _ble_should_pause(self) -> bool:
        """Protect voice, streamed playback, and OTA from combo-radio contention."""
        return bool(
            getattr(self.satellite, "_is_streaming_audio", False)
            or getattr(self.satellite, "_pipeline_active", False)
            or bool(getattr(self.client, "sendspin", None) and self.client.sendspin.status()["playing"])
            or self.overlay_session is not None
            or self.audio_scene is not None
            or (self.ota_task is not None and not self.ota_task.done())
        )

    def status(self) -> dict[str, Any]:
        self._reconcile_ringing_timers()
        wake_policy = self._wake_policy()
        wake_mode = self._wake_detector_mode()
        companion_status = self._oww_status()
        companion_engine = self._companion_engine()
        wake_engine_enabled = str(self.settings.get("wake_engine") or "micro_wake_word") not in {"off", "button"}
        mww_ready = wake_engine_enabled and bool(getattr(self.state, "active_wake_words", set()) or getattr(self.state, "wake_words", {}))
        if not wake_engine_enabled:
            wake_ready = False
        elif wake_mode == "mww":
            wake_ready = mww_ready
        elif wake_mode in {"oww", "nww"}:
            wake_ready = bool(companion_status.get("ready"))
        else:
            wake_ready = mww_ready and bool(companion_status.get("ready"))
        if wake_mode in {"mww_oww", "mww_nww"}:
            engine_name = wake_mode
        elif wake_mode == "oww":
            engine_name = "open_wake_word"
        elif wake_mode == "nww":
            engine_name = "nano_wake_word"
        else:
            engine_name = "micro_wake_word"
        inactive_companion = {"ready": False, "enabled": False}
        disk_free = 0
        try:
            disk_free = shutil.disk_usage(_SETTINGS_PATH.parent).free
        except OSError:
            pass
        return {
            "timer_count": len(self.timers),
            "timer_ringing": any(timer.ringing for timer in self.timers.values()),
            "sendspin": {
                "playing": _SENDSPIN_PLAYING_MARKER.exists(),
                "output_channel_mode": str(self.settings.get("output_channel_mode") or "stereo"),
            },
            "ota_active": self.ota_task is not None and not self.ota_task.done(),
            "wake_model_downloading": self.wake_model_downloading,
            "wake_sound_downloading": self.wake_sound_downloading,
            "wake_word": next(iter(getattr(self.state, "active_wake_words", set())), ""),
            "wake_sound": str(self.settings.get("wake_sound") or "no_sound"),
            "wake_engine": {
                "ready": wake_ready,
                "name": engine_name,
                "mode": wake_mode,
                "active_wake_word": next(iter(getattr(self.state, "active_wake_words", set())), ""),
                "source": self._oww_model_source if companion_engine else "embedded_or_custom",
                "policy": wake_policy,
                "openwakeword": companion_status if companion_engine == "open_wake_word" else inactive_companion,
                "nanowakeword": companion_status if companion_engine == "nano_wake_word" else inactive_companion,
                "verifier": self.client._wake_verifier_status() if getattr(self.client, "uses_shared_features", False) else self._wake_verifier_status(),  # pylint: disable=protected-access
            },
            "audio_frontend": getattr(self.state, "tater_audio_frontend", {}),
            "bluetooth": self.bluetooth.status(),
            "data_free_bytes": disk_free,
        }

    def handle_message(self, body: dict[str, Any]) -> bool:
        message_type = str(body.get("type") or "").strip()
        message_id = str(body.get("id") or "").strip()
        raw_payload = body.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}

        shared_client = bool(getattr(self.client, "uses_shared_features", False))
        if shared_client and (message_type.startswith(("timer.", "ble.", "bluetooth.")) or message_type in {"settings", "wake.verify.result"} or message_type.startswith("media.session.")):
            return False

        if self.bluetooth.handle_message(body):
            return True

        if message_type in _RETIRED_MEDIA_MESSAGES:
            return True

        if message_type == "voice.event":
            event_name = str(payload.get("event") or "").strip().upper()
            if event_name.startswith("VOICE_ASSISTANT_"):
                event_name = event_name[len("VOICE_ASSISTANT_") :]
            if event_name == "TOOL_CALL_START":
                self.satellite._emit(  # pylint: disable=protected-access
                    LVAEvent.LIGHT_COMMAND,
                    {"state": True, "effect": "tool_call"},
                )
            # The native client must still process the voice event.
            return False

        if message_type == "wake.verify.result":
            self._complete_wake_verification(
                _integer(payload.get("request_id"), maximum=0xFFFFFFFF),
                accepted=_truthy(payload.get("accepted")),
                available=_truthy(payload.get("available")),
                reason=str(payload.get("reason") or "")[:120],
            )
            return True
        if message_type in {"timer.start", "timer.arm"}:
            self._start_timer(payload, message_id, replace=message_type == "timer.arm")
            return True
        if message_type in {"timer.list", "timer.status"}:
            self._timer_result(message_id, "list", timers=[timer.public() for timer in self.timers.values()], count=len(self.timers))
            return True
        if message_type in {"timer.cancel", "timer.clear"}:
            self._cancel_timers(payload, message_id, clear_all=message_type == "timer.clear")
            return True
        if message_type == "timer.snooze":
            self._snooze_timers(payload, message_id)
            return True
        if message_type == "timer.alarm":
            for timer in self._select_timers(payload):
                self._ring_timer(timer)
            return True
        if message_type == "settings":
            self._apply_settings(payload, persist=True)
            return True
        if message_type == "audio.overlay.start":
            self._start_overlay(payload)
            return True
        if message_type == "audio.scene.start":
            self._start_audio_scene(payload)
            return True
        if message_type == "audio.scene.stop":
            self._stop_audio_scene(ok=False)
            return True
        if message_type in {"setup.reset", "provisioning.reset"}:
            asyncio.create_task(self._reset_setup())
            return True
        if message_type == "ota.url":
            if self.ota_task is None or self.ota_task.done():
                self.ota_task = asyncio.create_task(self._run_ota(payload))
            else:
                self._send("ota.status", {"status": "error", "progress": 0, "message": "An update is already running."})
            return True
        return False

    def _timer_result(self, reply_to: str, action: str, *, ok: bool = True, **extra: Any) -> None:
        self._send("timer.result", {"reply_to": reply_to, "action": action, "ok": ok, **extra})

    def _start_timer(self, payload: dict[str, Any], reply_to: str, *, replace: bool) -> None:
        duration_ms = _integer(
            payload.get("remaining_ms") or payload.get("duration_ms") or (_integer(payload.get("duration_s")) * 1000),
            maximum=_MAX_TIMER_SECONDS * 1000,
        )
        original_ms = _integer(payload.get("original_duration_ms") or duration_ms, maximum=_MAX_TIMER_SECONDS * 1000)
        timer_id = str(payload.get("id") or uuid.uuid4().hex[:12]).strip()[:47]
        name = str(payload.get("name") or payload.get("label") or "").strip()[:63]
        if duration_ms <= 0:
            self._timer_result(reply_to, "start", ok=False, code="invalid_duration", message="Timer duration must be greater than zero.")
            return
        existing = self.timers.get(timer_id)
        if existing is not None and not replace:
            self._timer_result(reply_to, "start", timer=existing.public(), code="already_exists")
            return
        if existing is None and len(self.timers) >= _MAX_TIMERS:
            self._timer_result(reply_to, "start", ok=False, code="timer_limit", message="This satellite already has the maximum number of timers.")
            return
        if existing is not None and existing.task is not None:
            existing.task.cancel()
        timer = _Timer(timer_id, name, original_ms, time.monotonic() + (duration_ms / 1000.0))
        timer.task = asyncio.create_task(self._timer_wait(timer))
        self.timers[timer_id] = timer
        self._timer_result(reply_to, "start", timer=timer.public())
        self._emit_timer_event("updated" if replace and existing is not None else "armed", timer)

    async def _timer_wait(self, timer: _Timer) -> None:
        try:
            await asyncio.sleep(max(0.0, timer.deadline - time.monotonic()))
        except asyncio.CancelledError:
            return
        if self.timers.get(timer.timer_id) is timer:
            self._ring_timer(timer)

    def _ring_timer(self, timer: _Timer) -> None:
        if timer.ringing:
            return
        timer.ringing = True
        timer.deadline = time.monotonic()
        self.satellite._timer_finished = True  # pylint: disable=protected-access
        self.satellite._timer_ring_start = time.monotonic()  # pylint: disable=protected-access
        self.state.active_wake_words.add(self.state.stop_word.id)
        self.satellite.duck()
        self.satellite._emit(LVAEvent.TIMER_RINGING, timer.public())  # pylint: disable=protected-access
        self.satellite._play_timer_finished()  # pylint: disable=protected-access
        self._emit_timer_event("expired", timer)

    def _select_timers(self, payload: dict[str, Any], *, clear_all: bool = False) -> list[_Timer]:
        timers = list(self.timers.values())
        if clear_all:
            return timers
        ids = {str(value) for value in payload.get("ids", []) if str(value).strip()} if isinstance(payload.get("ids"), list) else set()
        timer_id = str(payload.get("id") or payload.get("timer_id") or "").strip()
        name = str(payload.get("name") or payload.get("label") or "").strip().lower()
        if ids:
            return [timer for timer in timers if timer.timer_id in ids]
        if timer_id:
            return [timer for timer in timers if timer.timer_id == timer_id]
        if name:
            return [timer for timer in timers if timer.name.lower() == name]
        ringing = [timer for timer in timers if timer.ringing]
        return ringing if ringing else (timers if len(timers) == 1 else [])

    def _cancel_timers(self, payload: dict[str, Any], reply_to: str, *, clear_all: bool) -> None:
        selected = self._select_timers(payload, clear_all=clear_all)
        rows = [timer.public() for timer in selected]
        for timer in selected:
            if timer.task is not None:
                timer.task.cancel()
            self.timers.pop(timer.timer_id, None)
            self._emit_timer_event("cleared" if clear_all else "cancelled", timer, active=False)
        self._stop_ringing_if_idle()
        code = "" if selected else "not_found"
        self._timer_result(reply_to, "cancel", timers=rows, affected=len(rows), code=code)

    def _snooze_timers(self, payload: dict[str, Any], reply_to: str) -> None:
        duration_ms = _integer(payload.get("duration_ms") or (_integer(payload.get("duration_s"), 300) * 1000), 300000, maximum=_MAX_TIMER_SECONDS * 1000)
        selected = self._select_timers(payload)
        for timer in selected:
            if timer.task is not None:
                timer.task.cancel()
            timer.ringing = False
            timer.original_duration_ms = duration_ms
            timer.deadline = time.monotonic() + duration_ms / 1000.0
            timer.task = asyncio.create_task(self._timer_wait(timer))
            self._emit_timer_event("snoozed", timer)
        self._stop_ringing_if_idle()
        self._timer_result(reply_to, "snooze", timers=[timer.public() for timer in selected], affected=len(selected), code="" if selected else "not_found")

    def _emit_timer_event(self, event: str, timer: _Timer, *, active: bool = True) -> None:
        row = timer.public()
        row.update({"event": event, "active": active, "state": row["state"] if active else "stopped"})
        self._send("timer.event", row)

    def _reconcile_ringing_timers(self) -> None:
        if any(timer.ringing for timer in self.timers.values()) and not bool(getattr(self.satellite, "_timer_finished", False)):
            for timer in list(self.timers.values()):
                if timer.ringing:
                    self.timers.pop(timer.timer_id, None)
                    self._emit_timer_event("stopped", timer, active=False)

    def _stop_ringing_if_idle(self) -> None:
        if any(timer.ringing for timer in self.timers.values()):
            return
        if bool(getattr(self.satellite, "_timer_finished", False)):
            self.satellite._timer_finished = False  # pylint: disable=protected-access
            self.satellite._timer_ring_start = None  # pylint: disable=protected-access
            self.state.active_wake_words.discard(self.state.stop_word.id)
            self._player("tts_player").stop()
            self.satellite.unduck()
            self.satellite._emit(LVAEvent.IDLE)  # pylint: disable=protected-access

    def _apply_settings(self, payload: dict[str, Any], *, persist: bool) -> None:
        applied: dict[str, Any] = {}
        previous_output_channel = str(self.settings.get("output_channel_mode") or "stereo")
        if "volume_percent" in payload:
            volume = _integer(payload.get("volume_percent"), 80, maximum=100)
            self._player("music_player").set_volume(volume)
            self._player("tts_player").set_volume(volume)
            self.state.persist_volume(volume / 100.0)
            applied["volume_percent"] = volume
        if "muted" in payload:
            muted = _truthy(payload.get("muted"))
            self.satellite._set_muted(muted)  # pylint: disable=protected-access
            applied["muted"] = muted
            if muted:
                with self._wake_audio_lock:
                    self._wake_audio.clear()
        if "wake_threshold" in payload:
            try:
                threshold = float(payload["wake_threshold"])
            except (TypeError, ValueError):
                threshold = None
            if threshold is not None:
                threshold = max(0.01, min(0.99, threshold))
                applied["wake_threshold"] = threshold
        for threshold_key in ("oww_threshold", "nww_threshold"):
            if threshold_key not in payload:
                continue
            try:
                threshold = float(payload[threshold_key])
            except (TypeError, ValueError):
                continue
            if 0.01 <= threshold <= 1.0:
                applied[threshold_key] = threshold
        for patience_key in ("oww_patience", "nww_patience"):
            if patience_key not in payload:
                continue
            try:
                patience = int(payload[patience_key])
            except (TypeError, ValueError):
                continue
            if 1 <= patience <= 20:
                applied[patience_key] = patience
        if "wake_sensitivity" in payload:
            sensitivity = str(payload.get("wake_sensitivity") or "").strip().lower()
            normalized = _normalize_wake_sensitivity(sensitivity)
            if sensitivity in _WAKE_SENSITIVITIES or sensitivity in {
                "medium",
                "conservative",
                "sensitive",
                "very_sensitive",
            }:
                applied["wake_sensitivity"] = normalized
        if "wake_environment" in payload:
            environment = str(payload.get("wake_environment") or "").strip().lower()
            normalized = _normalize_wake_environment(environment)
            if environment in _WAKE_ENVIRONMENTS or environment in {
                "quiet",
                "quiet_room",
                "very_sensitive",
                "tv",
                "near_tv",
            }:
                applied["wake_environment"] = normalized
        if "wake_engine" in payload:
            engine = str(payload.get("wake_engine") or "micro_wake_word").strip()
            if engine in {"off", "button", "micro_wake_word"}:
                applied["wake_engine"] = engine
        if "wake_detector_mode" in payload:
            detector_mode = str(payload.get("wake_detector_mode") or "mww").strip().lower()
            if detector_mode == "dual":
                detector_mode = "mww_oww"
            if detector_mode in _WAKE_DETECTOR_MODES:
                applied["wake_detector_mode"] = detector_mode
                applied["wake_mww_enabled"] = detector_mode in {"mww", "mww_oww", "mww_nww"}
                applied["wake_oww_enabled"] = detector_mode in {"oww", "mww_oww"}
                applied["wake_nww_enabled"] = detector_mode in {"nww", "mww_nww"}
        elif {"wake_mww_enabled", "wake_oww_enabled", "wake_nww_enabled"}.intersection(payload):
            mww_enabled = _truthy(payload.get("wake_mww_enabled", self.settings.get("wake_mww_enabled", True)))
            oww_enabled = _truthy(payload.get("wake_oww_enabled", self.settings.get("wake_oww_enabled", False)))
            nww_enabled = _truthy(payload.get("wake_nww_enabled", self.settings.get("wake_nww_enabled", False)))
            detector_mode = "mww_nww" if mww_enabled and nww_enabled else "mww_oww" if mww_enabled and oww_enabled else "nww" if nww_enabled else "oww" if oww_enabled else "mww"
            applied.update(
                {
                    "wake_detector_mode": detector_mode,
                    "wake_mww_enabled": detector_mode in {"mww", "mww_oww", "mww_nww"},
                    "wake_oww_enabled": detector_mode in {"oww", "mww_oww"},
                    "wake_nww_enabled": detector_mode in {"nww", "mww_nww"},
                }
            )
        if "wake_word" in payload:
            wake_word = str(payload.get("wake_word") or "").strip()
            if wake_word and len(wake_word) <= 120:
                applied["wake_word"] = wake_word
        if "wake_word_url" in payload:
            wake_word_url = str(payload.get("wake_word_url") or "").strip()
            if len(wake_word_url) <= 2048:
                applied["wake_word_url"] = wake_word_url
        if "oww_wake_word" in payload:
            oww_wake_word = str(payload.get("oww_wake_word") or "").strip()
            if oww_wake_word and len(oww_wake_word) <= 120:
                applied["oww_wake_word"] = oww_wake_word
        if "oww_wake_word_url" in payload:
            oww_wake_word_url = str(payload.get("oww_wake_word_url") or "").strip()
            if len(oww_wake_word_url) <= 2048:
                applied["oww_wake_word_url"] = oww_wake_word_url
        if "nww_wake_word" in payload:
            nww_wake_word = str(payload.get("nww_wake_word") or "").strip()
            if nww_wake_word and len(nww_wake_word) <= 120:
                applied["nww_wake_word"] = nww_wake_word
        if "nww_wake_word_url" in payload:
            nww_wake_word_url = str(payload.get("nww_wake_word_url") or "").strip()
            if len(nww_wake_word_url) <= 2048:
                applied["nww_wake_word_url"] = nww_wake_word_url
        if "nww_model_revision" in payload:
            nww_model_revision = str(payload.get("nww_model_revision") or "").strip()
            if len(nww_model_revision) <= 128:
                applied["nww_model_revision"] = nww_model_revision
        if "nww_architecture" in payload:
            nww_architecture = str(payload.get("nww_architecture") or "cnn").strip().lower()
            if nww_architecture in _NWW_ARCHITECTURES:
                applied["nww_architecture"] = nww_architecture
        if "wake_sound_enabled" in payload:
            applied["wake_sound_enabled"] = _truthy(payload.get("wake_sound_enabled"))
        if "wake_sound" in payload:
            wake_sound = str(payload.get("wake_sound") or "").strip().lower()
            if wake_sound in _WAKE_SOUND_IDS:
                applied["wake_sound"] = wake_sound
        if "wake_sound_url" in payload:
            wake_sound_url = str(payload.get("wake_sound_url") or "").strip()
            if len(wake_sound_url) <= 2048:
                applied["wake_sound_url"] = wake_sound_url
        if "continued_chat" in payload:
            applied["continued_chat"] = _truthy(payload.get("continued_chat"))
        if "barge_in_enabled" in payload:
            applied["barge_in_enabled"] = _truthy(payload.get("barge_in_enabled"))
        if "wake_verifier_mode" in payload:
            verifier_mode = str(payload.get("wake_verifier_mode") or "off").strip().lower()
            if verifier_mode in _WAKE_VERIFIER_MODES:
                applied["wake_verifier_mode"] = verifier_mode
        if "wake_verifier_window_ms" in payload:
            applied["wake_verifier_window_ms"] = _integer(
                payload.get("wake_verifier_window_ms"),
                1000,
                minimum=500,
                maximum=2000,
            )
        if "wake_verifier_timeout_ms" in payload:
            applied["wake_verifier_timeout_ms"] = _integer(
                payload.get("wake_verifier_timeout_ms"),
                500,
                minimum=100,
                maximum=2000,
            )
        if "logging_level" in payload:
            level_name = str(payload.get("logging_level") or "info").strip().upper()
            level = getattr(logging, level_name, logging.INFO)
            logging.getLogger().setLevel(level)
            applied["logging_level"] = level_name.lower()
        if "output_channel_mode" in payload:
            output_channel = str(payload.get("output_channel_mode") or "stereo").strip().lower()
            if output_channel in _SENDSPIN_CHANNELS:
                applied["output_channel_mode"] = output_channel
        if "led_brightness" in payload:
            applied["led_brightness"] = _integer(payload.get("led_brightness"), 80, maximum=100)
        if "led_color" in payload:
            color = str(payload.get("led_color") or "").strip()
            if _LED_COLOR.fullmatch(color):
                applied["led_color"] = color.lower()
        for key in _LED_SETTING_KEYS[2:]:
            if key not in payload:
                continue
            animation = str(payload.get(key) or "").strip().lower()
            if animation in _LED_ANIMATIONS:
                applied[key] = animation
        if applied:
            self.settings.update(applied)
            if "wake_threshold" in applied:
                self._configured_wake_threshold = float(applied["wake_threshold"])
            if {"wake_threshold", "wake_sensitivity", "wake_environment"}.intersection(applied):
                # Preserve the user's unmodified base threshold across a
                # reboot. Preferences contain the effective policy threshold,
                # which may be intentionally more or less sensitive.
                self.settings["wake_threshold"] = self._configured_wake_threshold
            self.satellite._tater_barge_in_enabled = bool(getattr(self.state, "tater_playback_reference_aec", False)) and _truthy(  # pylint: disable=protected-access
                self.settings.get("barge_in_enabled")
            )
            if persist:
                self._save_settings()
                if "output_channel_mode" in applied and str(applied["output_channel_mode"]) != previous_output_channel:
                    self._restart_sendspin()
        policy_keys = {"wake_threshold", "wake_sensitivity", "wake_environment"}
        if policy_keys.intersection(applied) or not persist:
            self._apply_wake_policy(persist_preferences=persist and bool(policy_keys.intersection(applied)))
        if str(self.settings.get("wake_verifier_mode") or "off") == "off" and not self._wake_verification_required():
            self._fail_open_pending_wake_verification("verifier_disabled_fail_open")
            with self._wake_audio_lock:
                self._wake_audio.clear()
        if {"wake_word", "wake_word_url", "wake_engine"}.intersection(payload):
            self._apply_wake_selection()
        oww_keys = {
            "wake_detector_mode",
            "wake_mww_enabled",
            "wake_oww_enabled",
            "wake_nww_enabled",
            "oww_wake_word",
            "oww_wake_word_url",
            "nww_wake_word",
            "nww_wake_word_url",
            "nww_model_revision",
            "nww_architecture",
            "oww_threshold",
            "oww_patience",
            "nww_threshold",
            "nww_patience",
        }
        if not persist or oww_keys.intersection(payload):
            self._apply_oww_selection()
        if (not persist and not getattr(self.client, "uses_shared_features", False)) or {"wake_sound_enabled", "wake_sound", "wake_sound_url"}.intersection(payload):
            self._apply_wake_sound_selection()

    def _cancel_custom_wake_sound_download(self) -> None:
        self.wake_sound_generation += 1
        if self.wake_sound_task is not None and not self.wake_sound_task.done():
            self.wake_sound_task.cancel()
        self.wake_sound_task = None
        self.wake_sound_downloading = False

    def _apply_wake_sound_selection(self) -> None:
        enabled = _truthy(self.settings.get("wake_sound_enabled"))
        sound_id = str(self.settings.get("wake_sound") or "no_sound").strip().lower()
        if sound_id not in _WAKE_SOUND_IDS:
            sound_id = "no_sound"

        if (not enabled) or sound_id == "no_sound":
            self._cancel_custom_wake_sound_download()
            self.state.wakeup_sound = self.satellite._tater_wakeup_sound = ""  # pylint: disable=protected-access
            return

        if sound_id == "default":
            self._cancel_custom_wake_sound_download()
            self.state.wakeup_sound = self.satellite._tater_wakeup_sound = self._default_wakeup_sound  # pylint: disable=protected-access
            return

        if sound_id in _WAKE_SOUND_FILES:
            self._cancel_custom_wake_sound_download()
            sound_path = _TATER_WAKE_SOUND_DIR / _WAKE_SOUND_FILES[sound_id]
            if sound_path.is_file():
                self.state.wakeup_sound = self.satellite._tater_wakeup_sound = str(sound_path)  # pylint: disable=protected-access
            else:
                self.state.wakeup_sound = self.satellite._tater_wakeup_sound = ""  # pylint: disable=protected-access
                self._send(
                    "log",
                    {
                        "level": "error",
                        "message": f"Tater wake sound '{sound_id}' is missing from this firmware image.",
                    },
                )
            return

        custom_url = str(self.settings.get("wake_sound_url") or "").strip()
        if not custom_url:
            self._cancel_custom_wake_sound_download()
            self.state.wakeup_sound = self.satellite._tater_wakeup_sound = ""  # pylint: disable=protected-access
            self._send("log", {"level": "error", "message": "Custom wake sound selected without a WAV URL."})
            return

        try:
            cached_path = _cached_custom_wake_sound(custom_url)
        except Exception:  # pylint: disable=broad-except
            cached_path = None
        if cached_path is not None:
            self._cancel_custom_wake_sound_download()
            self.state.wakeup_sound = self.satellite._tater_wakeup_sound = str(cached_path)  # pylint: disable=protected-access
            return

        self._cancel_custom_wake_sound_download()
        generation = self.wake_sound_generation
        self.wake_sound_downloading = True
        self.satellite._tater_wakeup_sound = ""  # pylint: disable=protected-access
        self.wake_sound_task = asyncio.create_task(self._activate_custom_wake_sound(custom_url, generation))

    async def _activate_custom_wake_sound(self, custom_url: str, generation: int) -> None:
        self._send("log", {"level": "info", "message": "Downloading the Tater custom wake sound."})
        try:
            sound_path = await asyncio.to_thread(_download_custom_wake_sound, custom_url)
            if generation != self.wake_sound_generation:
                return
            if not _truthy(self.settings.get("wake_sound_enabled")):
                return
            if str(self.settings.get("wake_sound") or "") != "custom":
                return
            if str(self.settings.get("wake_sound_url") or "").strip() != custom_url:
                return
            self.state.wakeup_sound = self.satellite._tater_wakeup_sound = str(sound_path)  # pylint: disable=protected-access
            self._send("log", {"level": "info", "message": "The Tater custom wake sound is ready."})
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to install custom wake sound")
            self._send("log", {"level": "error", "message": f"Custom wake-sound download failed: {exc}"})
        finally:
            if generation == self.wake_sound_generation:
                self.wake_sound_downloading = False

    def _activate_wake_word(
        self,
        model_id: str,
        *,
        available: Any = None,
        loaded: Any = None,
    ) -> bool:
        if available is not None:
            self.state.available_wake_words[model_id] = available
        available = self.state.available_wake_words.get(model_id)
        if available is None:
            return False

        wake_words = getattr(self.state, "wake_words", None)
        if wake_words is None:
            wake_words = {}
            self.state.wake_words = wake_words
        if loaded is None:
            loaded = wake_words.get(model_id)
        if loaded is None:
            loaded = available.load()
        wake_words[model_id] = loaded

        self.state.preferences.active_wake_words = [model_id]
        engine = str(self.settings.get("wake_engine") or "micro_wake_word")
        self.state.active_wake_words = {model_id} if engine == "micro_wake_word" else set()
        self.state.wake_words_changed = True
        self.state.save_preferences()
        return True

    def _apply_wake_selection(self) -> None:
        engine = str(self.settings.get("wake_engine") or "micro_wake_word")
        wake_word = str(self.settings.get("wake_word") or "hey_tater").strip()
        wake_word_url = str(self.settings.get("wake_word_url") or "").strip()

        if engine in {"off", "button"}:
            self.state.active_wake_words.clear()
            self.state.wake_words_changed = True
            return
        if engine != "micro_wake_word":
            return
        if wake_word == "custom_url":
            if wake_word_url:
                self._schedule_custom_wake_word(wake_word_url)
            else:
                self._send("log", {"level": "error", "message": "Custom wake word selected without a model URL."})
            return

        self._cancel_custom_wake_download()
        try:
            if not self._activate_wake_word(wake_word):
                self._send("log", {"level": "error", "message": f"Wake word '{wake_word}' is not installed on this satellite."})
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to activate wake word %s", wake_word)
            self._send("log", {"level": "error", "message": f"Could not activate wake word '{wake_word}': {exc}"})

    def _cancel_oww_activation(self) -> None:
        self.oww_model_generation += 1
        if self.oww_model_task is not None and not self.oww_model_task.done():
            self.oww_model_task.cancel()
        self.oww_model_task = None
        self.oww_model_downloading = False

    def _disable_oww_worker(self) -> None:
        with self._oww_lock:
            worker = self._oww_worker
            self._oww_worker = None
        if worker is not None:
            worker.close()
        self._oww_mww_candidate = 0.0
        self._oww_candidate = 0.0
        self._oww_engine_name = ""
        self._oww_architecture = ""

    def _apply_oww_selection(self) -> None:
        mode = self._wake_detector_mode()
        companion_engine = self._companion_engine()
        engine = str(self.settings.get("wake_engine") or "micro_wake_word")
        if not companion_engine or engine in {"off", "button"}:
            self._cancel_oww_activation()
            self._disable_oww_worker()
            self._oww_model_name = ""
            self._oww_model_source = ""
            self._oww_last_error = ""
            return
        if companion_engine == "nano_wake_word":
            wake_word = str(self.settings.get("nww_wake_word") or "hey_tater").strip().lower()
            bundle_url = str(self.settings.get("nww_wake_word_url") or "").strip()
            architecture = str(self.settings.get("nww_architecture") or "cnn").strip().lower()
            label = "NanoWakeWord"
        else:
            wake_word = str(self.settings.get("oww_wake_word") or "hey_tater").strip().lower()
            bundle_url = str(self.settings.get("oww_wake_word_url") or "").strip()
            architecture = ""
            label = "openWakeWord"
        if wake_word not in {"custom_url", "paired_bundle"}:
            bundle_url = ""
        if wake_word in {"custom_url", "paired_bundle"} and not bundle_url:
            self._oww_last_error = f"Custom {label} selected without a bundle URL."
            self._send("log", {"level": "error", "message": self._oww_last_error})
            return
        self._cancel_oww_activation()
        generation = self.oww_model_generation
        self.oww_model_downloading = bool(bundle_url)
        self.oww_model_task = asyncio.create_task(
            self._activate_oww(
                bundle_url,
                companion_engine=companion_engine,
                architecture=architecture,
                confirmation=mode in {"mww_oww", "mww_nww"},
                generation=generation,
            )
        )

    async def _activate_oww(
        self,
        bundle_url: str,
        *,
        companion_engine: str,
        architecture: str,
        confirmation: bool,
        generation: int,
    ) -> None:
        label = "custom" if bundle_url else "built-in"
        engine_label = "NanoWakeWord" if companion_engine == "nano_wake_word" else "openWakeWord"
        architecture_label = f" {architecture.upper()}" if architecture else ""
        self._send("log", {"level": "info", "message": f"Loading the {label} ONNX {engine_label}{architecture_label} model."})
        new_worker: Optional[OWWWorker] = None
        try:
            if companion_engine == "nano_wake_word":
                if bundle_url:
                    package = await asyncio.to_thread(
                        _download_nww_package,
                        bundle_url,
                        architecture=architecture,
                    )
                else:
                    package = await asyncio.to_thread(
                        _load_builtin_nww_package,
                        architecture,
                    )
            else:
                if bundle_url:
                    package = await asyncio.to_thread(
                        _download_oww_package,
                        bundle_url,
                        confirmation=confirmation,
                    )
                else:
                    package = await asyncio.to_thread(
                        _load_builtin_oww_package,
                        confirmation=confirmation,
                    )
            threshold_key = "nww_threshold" if companion_engine == "nano_wake_word" else "oww_threshold"
            patience_key = "nww_patience" if companion_engine == "nano_wake_word" else "oww_patience"
            threshold = float(self.settings.get(threshold_key, package.threshold))
            patience = int(self.settings.get(patience_key, package.patience))
            threshold = max(0.01, min(1.0, threshold))
            patience = max(1, min(20, patience))
            worker_task = asyncio.create_task(asyncio.to_thread(OWWWorker, package.classifier_path, threshold, patience))
            try:
                new_worker = await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                # Cancellation cannot stop a constructor already running in
                # another thread. Reclaim its interpreter when it completes.
                abandoned = await worker_task
                await asyncio.to_thread(abandoned.close)
                raise
            if generation != self.oww_model_generation:
                await asyncio.to_thread(new_worker.close)
                return
            with self._oww_lock:
                previous = self._oww_worker
                self._oww_worker = new_worker
            new_worker = None
            if previous is not None:
                await asyncio.to_thread(previous.close)
            self._oww_model_name = package.wake_word
            self._oww_model_source = package.source
            self._oww_engine_name = package.engine
            self._oww_architecture = package.architecture
            self._oww_last_error = ""
            self._oww_mww_candidate = 0.0
            self._oww_candidate = 0.0
            self._send(
                "log",
                {
                    "level": "info",
                    "message": (f"ONNX {engine_label}{architecture_label} '{package.wake_word}' is ready " f"(threshold {threshold:.3f}, patience {patience})."),
                },
            )
        except asyncio.CancelledError:
            if new_worker is not None:
                await asyncio.to_thread(new_worker.close)
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if new_worker is not None:
                await asyncio.to_thread(new_worker.close)
            self._oww_last_error = str(exc)[:240]
            _LOGGER.exception("Unable to activate ONNX %s", engine_label)
            self._send("log", {"level": "error", "message": f"ONNX {engine_label} failed: {exc}"})
        finally:
            if generation == self.oww_model_generation:
                self.oww_model_downloading = False

    def _cancel_custom_wake_download(self) -> None:
        self.wake_model_generation += 1
        if self.wake_model_task is not None and not self.wake_model_task.done():
            self.wake_model_task.cancel()
        self.wake_model_task = None
        self.wake_model_downloading = False

    def _schedule_custom_wake_word(self, manifest_url: str) -> None:
        self._cancel_custom_wake_download()
        generation = self.wake_model_generation
        self.wake_model_downloading = True
        self.wake_model_task = asyncio.create_task(self._activate_custom_wake_word(manifest_url, generation))

    async def _activate_custom_wake_word(self, manifest_url: str, generation: int) -> None:
        self._send("log", {"level": "info", "message": "Downloading the Tater custom wake word."})
        try:
            model_id, available, loaded = await asyncio.to_thread(
                _download_custom_wake_package,
                self.state,
                manifest_url,
            )
            if generation != self.wake_model_generation:
                return
            if str(self.settings.get("wake_word") or "") != "custom_url":
                return
            if str(self.settings.get("wake_word_url") or "").strip() != manifest_url:
                return
            if self._activate_wake_word(model_id, available=available, loaded=loaded):
                phrase = str(getattr(available, "wake_word", "custom wake word") or "custom wake word")
                self._send("log", {"level": "info", "message": f"Tater wake word '{phrase}' is ready."})
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to install custom wake word")
            self._send("log", {"level": "error", "message": f"Custom wake-word download failed: {exc}"})
        finally:
            if generation == self.wake_model_generation:
                self.wake_model_downloading = False

    def _start_overlay(self, payload: dict[str, Any]) -> None:
        foreground: Any = payload.get("foreground") if isinstance(payload.get("foreground"), dict) else payload
        ducking: Any = payload.get("ducking") if isinstance(payload.get("ducking"), dict) else {}
        overlay_id = str(payload.get("overlay_id") or uuid.uuid4().hex).strip()
        url = str(foreground.get("url") or "").strip()
        if not url:
            self._send("audio.overlay.finished", {"overlay_id": overlay_id, "ok": False})
            return
        if self.audio_scene is not None:
            self._stop_audio_scene(ok=False)
        if self.overlay_session is not None:
            self._stop_overlay(ok=False)
        self._overlay_generation += 1
        session = _OverlaySession(
            overlay_id=overlay_id,
            url=url,
            group_id=str(payload.get("group_id") or "").strip(),
            start_at_us=_integer(payload.get("start_at_us"), maximum=2**63 - 1),
            volume_percent=_integer(foreground.get("volume_percent"), 100, maximum=100),
            duck_target=_integer(ducking.get("target_percent"), 20, maximum=100) / 100.0,
            attack_ms=_integer(ducking.get("attack_ms"), 150, maximum=10000),
            release_ms=_integer(ducking.get("release_ms"), 350, maximum=10000),
            continue_conversation=_truthy(payload.get("continue_conversation")),
            generation=self._overlay_generation,
        )
        self.overlay_session = session
        session.task = asyncio.create_task(self._run_overlay(session))

    def _set_reply_light(self, active: bool) -> None:
        payload: dict[str, Any] = {"state": active}
        if active:
            payload["effect"] = "speaking"
        self.satellite._emit(  # pylint: disable=protected-access
            LVAEvent.LIGHT_COMMAND,
            payload,
        )

    async def _run_overlay(self, session: _OverlaySession) -> None:
        loop = asyncio.get_running_loop()
        playback_finished: asyncio.Future[None] = loop.create_future()

        def finished() -> None:
            def resolve() -> None:
                if not playback_finished.done():
                    playback_finished.set_result(None)

            self._call_soon(resolve)

        try:
            self._player("tts_player").set_volume(session.volume_percent)
            if not self._sync_overlay_available:
                await self._ramp_music_duck(session.duck_target, session.attack_ms)
                self._player("tts_player").play(session.url, done_callback=finished, stop_first=True)
                self._set_reply_light(True)
                session.started = True
                actual_start_us = time.monotonic_ns() // 1000
                self._send(
                    "audio.overlay.started",
                    {
                        "overlay_id": session.overlay_id,
                        "group_id": session.group_id,
                        "scheduled_start_us": session.start_at_us,
                        "actual_start_us": actual_start_us,
                        "late_by_us": max(0, actual_start_us - session.start_at_us) if session.start_at_us else 0,
                    },
                )
            else:
                self._player("tts_player").prepare_synchronized(
                    session.url,
                    channel="mono",
                    loop=False,
                    done_callback=finished,
                )
                await self._wait_player_ready(self._player("tts_player"), _AUDIO_PREPARE_TIMEOUT_SECONDS)
                output_latency_frames = _MEDIA_DEFAULT_OUTPUT_LATENCY_FRAMES
                latency_us = int(round(output_latency_frames * 1_000_000.0 / _MEDIA_SAMPLE_RATE_HZ))
                audible_deadline_us = session.start_at_us or ((time.monotonic_ns() // 1000) + latency_us)
                resume_at_us = audible_deadline_us - latency_us
                await self._sleep_until_us(resume_at_us - (session.attack_ms * 1000))
                await self._ramp_music_duck(session.duck_target, session.attack_ms)
                await self._sleep_until_us(resume_at_us)
                if self.overlay_session is not session:
                    return
                self._player("tts_player").resume()
                self._set_reply_light(True)
                actual_resume_us = time.monotonic_ns() // 1000
                actual_audible_us = actual_resume_us + latency_us
                session.started = True
                self._send(
                    "audio.overlay.started",
                    {
                        "overlay_id": session.overlay_id,
                        "group_id": session.group_id,
                        "scheduled_start_us": audible_deadline_us,
                        "actual_start_us": actual_audible_us,
                        "late_by_us": actual_audible_us - audible_deadline_us,
                        "output_latency_frames": output_latency_frames,
                    },
                )

            if self._sync_overlay_available:
                completed = await self._wait_for_playback_completion(
                    playback_finished,
                    (("TTS overlay", self._player("tts_player")),),
                    lambda: self.overlay_session is session,
                )
                if not completed:
                    return
            else:
                await playback_finished
            if self.overlay_session is not session:
                return
            await self._ramp_music_duck(1.0, session.release_ms)
            if self._sync_overlay_available:
                self._player("tts_player").reset_synchronized()
            session.finished = True
            self.overlay_session = None
            self._set_reply_light(False)
            self._send(
                "audio.overlay.finished",
                {
                    "overlay_id": session.overlay_id,
                    "group_id": session.group_id,
                    "ok": True,
                    "continue_conversation": session.continue_conversation,
                },
            )
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except _PlaybackStalledError as exc:
            _LOGGER.warning("Playback watchdog stopped overlay %s: %s", session.overlay_id, exc)
            self._send(
                "log",
                {
                    "level": "warn",
                    "message": f"Playback watchdog reset stalled TTS overlay {session.overlay_id}: {exc}.",
                },
            )
            if self.overlay_session is session:
                self._finish_overlay_failure(session)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to play Tater audio overlay")
            if self.overlay_session is session:
                self._finish_overlay_failure(session)

    def _finish_overlay_failure(self, session: _OverlaySession) -> None:
        session.finished = True
        self.overlay_session = None
        self._set_reply_light(False)
        self._set_music_duck(1.0)
        try:
            self._player("tts_player").stop()
            if self._sync_overlay_available:
                self._player("tts_player").reset_synchronized()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to reset failed overlay player")
        self._send(
            "audio.overlay.finished",
            {"overlay_id": session.overlay_id, "group_id": session.group_id, "ok": False},
        )

    def _stop_overlay(self, *, ok: bool, notify: bool = True) -> None:
        session = self.overlay_session
        if session is None:
            return
        self._overlay_generation += 1
        self.overlay_session = None
        session.finished = True
        self._set_reply_light(False)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if session.task is not None and session.task is not current and not session.task.done():
            session.task.cancel()
        try:
            self._player("tts_player").stop()
            if self._sync_overlay_available:
                self._player("tts_player").reset_synchronized()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to stop audio overlay")
        self._set_music_duck(1.0)
        if notify:
            self._send(
                "audio.overlay.finished",
                {"overlay_id": session.overlay_id, "group_id": session.group_id, "ok": ok},
            )

    def _start_audio_scene(self, payload: dict[str, Any]) -> None:
        foreground: Any = payload.get("foreground") if isinstance(payload.get("foreground"), dict) else {}
        background: Any = payload.get("background") if isinstance(payload.get("background"), dict) else {}
        ducking: Any = payload.get("ducking") if isinstance(payload.get("ducking"), dict) else {}
        finish: Any = payload.get("finish") if isinstance(payload.get("finish"), dict) else {}
        scene_id = str(payload.get("scene_id") or uuid.uuid4().hex).strip()
        foreground_url = str(foreground.get("url") or "").strip()
        if not foreground_url or not self._sync_overlay_available:
            self._send("audio.scene.finished", {"scene_id": scene_id, "ok": False})
            return
        if self.audio_scene is not None:
            self._stop_audio_scene(ok=False)
        if self.overlay_session is not None:
            self._stop_overlay(ok=False)
        self._scene_generation += 1
        scene = _AudioScene(
            scene_id=scene_id,
            foreground_url=foreground_url,
            background_url=str(background.get("url") or "").strip(),
            foreground_volume_percent=_integer(foreground.get("volume_percent"), 100, maximum=100),
            background_volume_percent=_integer(background.get("volume_percent"), 60, maximum=100),
            background_loop=_truthy(background.get("loop", True)),
            duck_target=_integer(ducking.get("target_percent"), 20, maximum=100) / 100.0,
            attack_ms=_integer(ducking.get("attack_ms"), 150, maximum=10000),
            release_ms=_integer(ducking.get("release_ms"), 350, maximum=10000),
            fade_ms=_integer(finish.get("fade_ms"), 350, maximum=10000),
            generation=self._scene_generation,
        )
        self.audio_scene = scene
        scene.task = asyncio.create_task(self._run_audio_scene(scene))

    async def _run_audio_scene(self, scene: _AudioScene) -> None:
        loop = asyncio.get_running_loop()
        foreground_finished: asyncio.Future[None] = loop.create_future()

        def finished() -> None:
            def resolve() -> None:
                if not foreground_finished.done():
                    foreground_finished.set_result(None)

            self._call_soon(resolve)

        try:
            self._player("tts_player").set_volume(scene.foreground_volume_percent)
            self._player("tts_player").prepare_synchronized(
                scene.foreground_url,
                channel="mono",
                loop=False,
                done_callback=finished,
            )
            if scene.background_url:
                self._player("music_player").set_volume(scene.background_volume_percent)
                self._player("music_player").prepare_synchronized(
                    scene.background_url,
                    channel="stereo",
                    loop=scene.background_loop,
                )
            await self._wait_player_ready(self._player("tts_player"), _AUDIO_PREPARE_TIMEOUT_SECONDS)
            if scene.background_url:
                await self._wait_player_ready(self._player("music_player"), _AUDIO_PREPARE_TIMEOUT_SECONDS)
            if self.audio_scene is not scene:
                return

            if scene.background_url:
                self._set_music_duck(1.0)
                self._player("music_player").resume()
                await self._ramp_music_duck(scene.duck_target, scene.attack_ms)
            self._player("tts_player").resume()
            self._set_reply_light(True)
            scene.started = True
            watched_players = [("audio-scene speech", self._player("tts_player"))]
            if scene.background_url and scene.background_loop:
                watched_players.append(("audio-scene background", self._player("music_player")))
            completed = await self._wait_for_playback_completion(
                foreground_finished,
                tuple(watched_players),
                lambda: self.audio_scene is scene,
            )
            if not completed:
                return
            if self.audio_scene is not scene:
                return

            if scene.background_url:
                await self._ramp_music_duck(1.0, scene.release_ms)
                await self._ramp_music_duck(0.0, scene.fade_ms)
                self._player("music_player").stop()
            self._player("tts_player").reset_synchronized()
            self._player("music_player").reset_synchronized()
            self._set_music_duck(1.0)
            scene.finished = True
            self.audio_scene = None
            self._set_reply_light(False)
            self._send("audio.scene.finished", {"scene_id": scene.scene_id, "ok": True})
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except _PlaybackStalledError as exc:
            _LOGGER.warning("Playback watchdog stopped audio scene %s: %s", scene.scene_id, exc)
            self._send(
                "log",
                {
                    "level": "warn",
                    "message": f"Playback watchdog reset stalled audio scene {scene.scene_id}: {exc}.",
                },
            )
            if self.audio_scene is scene:
                self._stop_audio_scene(ok=False)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to play Tater audio scene")
            if self.audio_scene is scene:
                self._stop_audio_scene(ok=False)

    def _stop_audio_scene(self, *, ok: bool, notify: bool = True) -> None:
        scene = self.audio_scene
        if scene is None:
            return
        self._scene_generation += 1
        self.audio_scene = None
        scene.finished = True
        self._set_reply_light(False)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if scene.task is not None and scene.task is not current and not scene.task.done():
            scene.task.cancel()
        try:
            self._player("tts_player").stop()
            self._player("music_player").stop()
            self._player("tts_player").reset_synchronized()
            self._player("music_player").reset_synchronized()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unable to stop audio scene")
        self._set_music_duck(1.0)
        if notify:
            self._send("audio.scene.finished", {"scene_id": scene.scene_id, "ok": ok})

    async def close(self) -> None:
        await self.bluetooth.close()
        if self.wake_verifier_timeout_task is not None and not self.wake_verifier_timeout_task.done():
            self.wake_verifier_timeout_task.cancel()
            try:
                await self.wake_verifier_timeout_task
            except asyncio.CancelledError:
                pass
        self.wake_verifier_timeout_task = None
        with self._wake_audio_lock:
            self._wake_audio.clear()
        if self.audio_scene is not None:
            self._stop_audio_scene(ok=False, notify=False)
        if self.overlay_session is not None:
            self._stop_overlay(ok=False, notify=False)
        for timer in self.timers.values():
            if timer.task is not None:
                timer.task.cancel()
        if self.ota_task is not None and not self.ota_task.done():
            self.ota_task.cancel()
        if self.wake_model_task is not None and not self.wake_model_task.done():
            self.wake_model_task.cancel()
            try:
                await self.wake_model_task
            except asyncio.CancelledError:
                pass
        if self.oww_model_task is not None and not self.oww_model_task.done():
            self.oww_model_task.cancel()
            try:
                await self.oww_model_task
            except asyncio.CancelledError:
                pass
        self.oww_model_task = None
        with self._oww_lock:
            oww_worker = self._oww_worker
            self._oww_worker = None
        if oww_worker is not None:
            await asyncio.to_thread(oww_worker.close)
        if self.wake_sound_task is not None and not self.wake_sound_task.done():
            self.wake_sound_task.cancel()
            try:
                await self.wake_sound_task
            except asyncio.CancelledError:
                pass
