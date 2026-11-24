"""Replay the bundle endpoint model and retain command audio across wake setup.

All positions are PCM sample counters, not wall clocks. The microphone keeps
capturing while the endpoint model, wake check and chime run.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .atomic_file import write_bytes_atomic

_LOGGER = logging.getLogger(__name__)
RATE = 16000
GUARD_SAMPLES = 1280  # 80 ms, shared with Native and Echo


def select_boundary(scores: list[tuple[int, float]], detected: int, settings: dict[str, Any]) -> tuple[int, float] | None:
    """Leading crossing of the strongest contiguous run, with command overlap."""
    threshold = float(settings.get("threshold", 0.82))
    start = detected - int(settings.get("search_before_ms", 700)) * 16
    end = detected + int(settings.get("search_after_ms", 120)) * 16
    run = None
    best: tuple[int, float] | None = None
    best_score = float("-inf")
    for position, score in scores:
        if not start <= position <= end or not score >= threshold:
            run = None
            continue
        if run is None:
            run = position
        if score > best_score:
            best_score = score
            best = (run + int(settings.get("endpoint_offset_ms", 0)) * 16 - GUARD_SAMPLES, score)
    return best


def boundary_probability(model, features, pending) -> float | None:
    """Read an endpoint score with both pymicro-wakeword 2.2 and newer APIs.

    S420 ships 2.2.1, whose public streaming method returns only a boolean.
    Use its already-bound TFLite C API and honor the model's tensor dtypes.
    """
    import numpy as np

    pending.append(features)
    if len(pending) < model.stride:
        return None
    types = {3: np.uint8, 9: np.int8}
    input_type = types[model.lib.TfLiteTensorType(model.input_tensor)]
    output_type = types[model.lib.TfLiteTensorType(model.output_tensor)]
    values = np.concatenate(pending, axis=1)
    pending.clear()
    quantized = np.clip(np.round(values / model.input_scale + model.input_zero_point), np.iinfo(input_type).min, np.iinfo(input_type).max).astype(input_type)
    if quantized.nbytes != model.lib.TfLiteTensorByteSize(model.input_tensor):
        raise ValueError("boundary input shape mismatch")
    if model.lib.TfLiteTensorCopyFromBuffer(model.input_tensor, quantized.ctypes.data_as(ctypes.c_void_p), quantized.nbytes):
        raise RuntimeError("boundary input copy failed")
    if model.lib.TfLiteInterpreterInvoke(model.interpreter):
        raise RuntimeError("boundary inference failed")
    output = np.empty(model.lib.TfLiteTensorByteSize(model.output_tensor), dtype=output_type)
    if output.size != 1:
        raise ValueError("boundary output shape mismatch")
    if model.lib.TfLiteTensorCopyToBuffer(model.output_tensor, output.ctypes.data_as(ctypes.c_void_p), output.nbytes):
        raise RuntimeError("boundary output copy failed")
    return float((output.astype(np.float32) - model.output_zero_point).item() * model.output_scale)


class WakeBoundaryModel:
    """One interpreter owned by the background boundary worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: Any = None
        self._key: tuple[str, str] | None = None
        self.last_status: dict[str, Any] = {"ready": False, "reason": "not_loaded"}

    def estimate(self, pcm: bytes, detected: int, bundle_path: Path) -> tuple[int, float] | None:
        from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

        with self._lock:
            try:
                raw = bundle_path.read_bytes()
                bundle = json.loads(raw)
                settings = bundle.get("wake_boundary") or {}
                model_name = str(settings.get("model") or "")
                if Path(model_name).name != model_name or not model_name:
                    raise ValueError("bundle has no local boundary model")
                path = bundle_path.parent / model_name
                data = path.read_bytes()
                if len(data) > 2 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != settings.get("model_sha256"):
                    raise ValueError("boundary model SHA-256 mismatch")
                if int(settings.get("sample_rate", RATE)) != RATE:
                    raise ValueError("unsupported boundary sample rate")
                if settings.get("model_size_bytes") and len(data) != int(settings["model_size_bytes"]):
                    raise ValueError("boundary model size mismatch")
                key = (str(path), hashlib.sha256(data).hexdigest())
                if key != self._key:
                    if self._model is not None:
                        self._model.close()
                    # The existing MWW loader resolves the platform's TFLite C
                    # runtime. A tiny generated config avoids a second runtime.
                    from pymicro_wakeword.microwakeword import _MODULE_LIB_DIR, _REPO_LIB_DIR, get_platform

                    library = next(iter(_MODULE_LIB_DIR.glob("*tensorflowlite_c.*")), None)
                    if library is None:
                        library = next(iter((_REPO_LIB_DIR / (get_platform() or "")).glob("*tensorflowlite_c.*")), None)
                    if library is None:
                        raise RuntimeError("TFLite C runtime unavailable")
                    self._model = MicroWakeWord(
                        "wake_boundary",
                        wake_word=bundle.get("wake_word", ""),
                        tflite_model=path,
                        probability_cutoff=float(settings.get("threshold", 0.82)),
                        sliding_window_size=1,
                        trained_languages=[],
                        libtensorflowlite_c_path=library,
                    )
                    self._key = key
                else:
                    self._model.reset()
                frontend = MicroWakeWordFeatures()
                scores = []
                pending: list[Any] = []
                # Explicit 10 ms steps retain the feature's exact sample clock.
                for offset in range(0, len(pcm) - 319, 320):
                    for features in frontend.process_streaming(pcm[offset : offset + 320]):
                        probability = boundary_probability(self._model, features, pending)
                        if probability is not None:
                            scores.append(((offset + 320) // 2, float(probability)))
                result = select_boundary(scores, detected, settings)
                self.last_status = {"ready": True, "model": model_name, "reason": "estimated" if result else "no_endpoint", "score": result[1] if result else 0.0}
                return result
            except Exception as exc:  # keep all audio if an endpoint is unavailable
                self.last_status = {"ready": False, "reason": str(exc)[:200]}
                _LOGGER.debug("Wake boundary unavailable: %s", exc)
                return None

    def close(self) -> None:
        with self._lock:
            if self._model is not None:
                self._model.close()
                self._model = None
            self._key = None


class WakeAudioBuffer:
    """Bounded mono ring and an independent wake capture; safe across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ring = bytearray()
        self._capture: bytearray | None = None
        self.detected = 0
        self.overflow = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._capture is not None

    def feed(self, pcm: bytes) -> bool:
        pcm = pcm[: len(pcm) // 2 * 2]
        with self._lock:
            self._ring.extend(pcm)
            del self._ring[: -RATE * 3 * 2]
            if self._capture is not None:
                self._capture.extend(pcm)
                if len(self._capture) > RATE * 12 * 2:
                    # Never silently drop the start of a command.
                    self.overflow = True
                    del self._capture[RATE * 12 * 2 :]
            return self._capture is not None

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()
            self._capture = None

    def trigger(self) -> None:
        with self._lock:
            if self._capture is not None:
                return
            self._capture = bytearray(self._ring[-RATE * 3 :])  # 1500 ms replay
            self.detected = len(self._capture) // 2
            self.overflow = False

    def snapshot(self) -> tuple[bytes, int]:
        with self._lock:
            # Endpoint search only needs 120 ms after the trigger, but retain
            # every later command sample for streaming independently.
            limit = (self.detected + 1920) * 2
            return bytes((self._capture or b"")[:limit]), self.detected

    def take(self) -> bytes | None:
        with self._lock:
            captured = bytes(self._capture) if self._capture is not None else None
            self._capture = None
            return captured

    def cancel(self) -> None:
        self.take()


def cache_boundary_bundle(bundle: dict, source_url: str, destination: Path, download) -> None:
    """Cache an optional companion endpoint only after verifying its identity."""
    from urllib.parse import urljoin, urlparse

    settings = bundle.get("wake_boundary")
    if not isinstance(settings, dict) or not settings.get("model"):
        return
    url = urljoin(source_url, str(settings["model"]))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("boundary model URL must use HTTP or HTTPS")
    model = download(url)
    if hashlib.sha256(model).hexdigest() != settings.get("model_sha256"):
        raise ValueError("boundary model SHA-256 mismatch")
    if settings.get("model_size_bytes") and len(model) != int(settings["model_size_bytes"]):
        raise ValueError("boundary model size mismatch")
    if len(model) > 2 * 1024 * 1024:
        raise ValueError("boundary model exceeds the size limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Content-addressed model names prevent an interrupted/stale update from
    # changing the model referenced by an already committed manifest.
    name = hashlib.sha256(model).hexdigest() + ".boundary.tflite"
    model_path = destination.parent / name
    write_bytes_atomic(model_path, model)
    cached = {**bundle, "wake_boundary": {**settings, "model": name}}
    write_bytes_atomic(destination, (json.dumps(cached, indent=2) + "\n").encode("utf-8"))
