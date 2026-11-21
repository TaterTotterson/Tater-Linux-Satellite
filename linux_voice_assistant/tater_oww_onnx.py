"""Asynchronous ONNX openWakeWord/NanoWakeWord lane for Tater satellites."""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import queue
import sys
import threading
import time
from array import array
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)
_BRIDGE_PATH = Path("/usr/lib/libtater_oww_ort.so")
_RUNTIME_PATH = Path("/usr/lib/libonnxruntime.so.1")
_SHARED_MODEL_DIR = Path(os.environ.get("TATER_WAKE_ASSET_DIR") or Path(__file__).parent / "assets" / "wake")
_MELSPEC_PATH = _SHARED_MODEL_DIR / "melspectrogram.onnx"
_EMBEDDING_PATH = _SHARED_MODEL_DIR / "embedding_model.onnx"


class OWWRuntimeError(RuntimeError):
    """Raised when the native OWW lane cannot be created or advanced."""


class _NativeEngine:
    def __init__(self, classifier_path: Path) -> None:
        self._library = ctypes.CDLL(str(_BRIDGE_PATH))
        self._bind()
        error = ctypes.c_void_p()
        self._handle = self._library.tater_oww_create(
            os.fsencode(_RUNTIME_PATH),
            os.fsencode(_MELSPEC_PATH),
            os.fsencode(_EMBEDDING_PATH),
            os.fsencode(classifier_path),
            ctypes.byref(error),
        )
        if not self._handle:
            raise OWWRuntimeError(self._take_error(error) or "could not create ONNX OWW engine")
        version = self._library.tater_oww_version(self._handle)
        self.version = version.decode("utf-8", errors="replace") if version else ""

    def _bind(self) -> None:
        lib = self._library
        lib.tater_oww_create.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.tater_oww_create.restype = ctypes.c_void_p
        lib.tater_oww_version.argtypes = [ctypes.c_void_p]
        lib.tater_oww_version.restype = ctypes.c_char_p
        lib.tater_oww_push.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.tater_oww_push.restype = ctypes.c_int
        lib.tater_oww_reset.argtypes = [ctypes.c_void_p]
        lib.tater_oww_destroy.argtypes = [ctypes.c_void_p]
        lib.tater_oww_free_error.argtypes = [ctypes.c_void_p]

    def _take_error(self, pointer: ctypes.c_void_p) -> str:
        if not pointer.value:
            return ""
        try:
            return ctypes.string_at(pointer.value).decode("utf-8", errors="replace")
        finally:
            self._library.tater_oww_free_error(pointer)

    def push(self, pcm: bytes) -> list[float]:
        if not self._handle:
            raise OWWRuntimeError("ONNX OWW engine is closed")
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return []
        sample_buffer = (ctypes.c_int16 * len(samples)).from_buffer(samples)
        scores = (ctypes.c_float * 16)()  # pylint: disable=no-value-for-parameter
        score_count = ctypes.c_size_t(0)
        error = ctypes.c_void_p()
        ok = self._library.tater_oww_push(
            self._handle,
            sample_buffer,
            len(samples),
            scores,
            len(scores),
            ctypes.byref(score_count),
            ctypes.byref(error),
        )
        if not ok:
            raise OWWRuntimeError(self._take_error(error) or "ONNX OWW inference failed")
        return [float(scores[index]) for index in range(score_count.value)]

    def reset(self) -> None:
        if self._handle:
            self._library.tater_oww_reset(self._handle)

    def close(self) -> None:
        if self._handle:
            self._library.tater_oww_destroy(self._handle)
            self._handle = None


class _PythonEngine:
    """Portable counterpart of the S420 ONNX C bridge (same 80 ms frontend)."""

    def __init__(self, classifier_path: Path) -> None:
        import numpy as np
        import onnxruntime as ort

        self.np = np
        self.version = ort.__version__
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.models = [ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"]) for path in (_MELSPEC_PATH, _EMBEDDING_PATH, classifier_path)]
        self.pending = bytearray()
        self.context: Any = None
        self.mel: Any = None
        self.features: Any = None
        self.reset()

    def reset(self) -> None:
        self.pending = bytearray()
        self.context = self.np.empty(0, dtype=self.np.float32)
        self.mel = self.np.ones((76, 32), dtype=self.np.float32)
        self.features = self.np.empty((0, 96), dtype=self.np.float32)

    @staticmethod
    def _run(model, values):
        return model.run(None, {model.get_inputs()[0].name: values})[0]

    def push(self, pcm: bytes) -> list[float]:
        np = self.np
        self.pending.extend(pcm)
        scores = []
        while len(self.pending) >= 2560:
            chunk = np.frombuffer(bytes(self.pending[:2560]), dtype="<i2").astype(np.float32)
            del self.pending[:2560]
            audio = np.concatenate((self.context, chunk))[None, :]
            mel = self._run(self.models[0], audio).reshape(-1, 32) / 10.0 + 2.0
            self.mel = np.concatenate((self.mel, mel))[-76:]
            embedding = self._run(self.models[1], self.mel.reshape(1, 76, 32, 1)).reshape(1, 96)
            self.features = np.concatenate((self.features, embedding))[-16:]
            self.context = chunk[-480:].copy()
            if len(self.features) < 16:
                continue
            score = float(self._run(self.models[2], self.features[None, :, :]).item())
            if not np.isfinite(score) or not 0 <= score <= 1:
                raise OWWRuntimeError("invalid ONNX classifier output")
            scores.append(score)
        return scores

    def close(self) -> None:
        self.models.clear()


class OWWWorker:
    """Run one OWW or NWW classifier on a core-pinned latest-wins worker."""

    def __init__(self, classifier_path: Path, threshold: float, patience: int) -> None:
        self.threshold = max(0.01, min(1.0, float(threshold)))
        self.patience = max(1, min(20, int(patience)))
        self._engine = (_NativeEngine if _BRIDGE_PATH.is_file() and _RUNTIME_PATH.is_file() else _PythonEngine)(classifier_path)
        self.runtime_version = self._engine.version
        self._audio: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self._events: queue.SimpleQueue[tuple[float, float]] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._reset_requested = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run_owned, name="tater-oww", daemon=True)
        self._submitted = 0
        self._processed = 0
        self._dropped = 0
        self._errors = 0
        self._detections = 0
        self._last_score = 0.0
        self._last_inference_ms = 0.0
        self._max_inference_ms = 0.0
        self._last_error = ""
        self._cpu: Optional[int] = None
        self._thread.start()

    def submit(self, pcm: bytes) -> None:
        if self._stop.is_set() or not pcm:
            return
        with self._lock:
            self._submitted += 1
        try:
            self._audio.put_nowait(bytes(pcm))
        except queue.Full:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped += 1
            self._reset_requested.set()
            try:
                self._audio.put_nowait(bytes(pcm))
            except queue.Full:
                with self._lock:
                    self._dropped += 1

    def pop_detection(self) -> Optional[tuple[float, float]]:
        latest: Optional[tuple[float, float]] = None
        while True:
            try:
                latest = self._events.get_nowait()
            except queue.Empty:
                return latest

    def reset(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                break
        self._reset_requested.set()
        while self.pop_detection() is not None:
            pass

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._audio.put_nowait(None)
        except queue.Full:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio.put_nowait(None)
            except queue.Full:
                pass
        self._thread.join(timeout=2.0)
        # The worker owns the interpreter; never destroy it during an inference.

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": not self._stop.is_set() and self._thread.is_alive(),
                "runtime": "onnxruntime",
                "runtime_version": self.runtime_version,
                "threshold": self.threshold,
                "patience": self.patience,
                "worker_cpu": self._cpu,
                "submitted": self._submitted,
                "processed": self._processed,
                "dropped": self._dropped,
                "errors": self._errors,
                "detections": self._detections,
                "last_score": round(self._last_score, 5),
                "last_inference_ms": round(self._last_inference_ms, 2),
                "max_inference_ms": round(self._max_inference_ms, 2),
                "last_error": self._last_error,
            }

    def _run_owned(self) -> None:
        try:
            self._run()
        finally:
            self._engine.close()

    def _run(self) -> None:
        cpu_count = os.cpu_count() or 1
        if cpu_count >= 4 and hasattr(os, "sched_setaffinity"):
            try:
                cpu = cpu_count - 1
                os.sched_setaffinity(0, {cpu})  # pylint: disable=no-member
                self._cpu = cpu
            except OSError:
                _LOGGER.debug("Could not pin the OWW worker", exc_info=True)
        consecutive = 0
        refractory_until = 0.0
        while not self._stop.is_set():
            try:
                pcm = self._audio.get(timeout=0.25)
            except queue.Empty:
                continue
            if pcm is None:
                break
            if self._reset_requested.is_set():
                self._engine.reset()
                self._reset_requested.clear()
                consecutive = 0
            started = time.monotonic()
            try:
                scores = self._engine.push(pcm)
                elapsed_ms = (time.monotonic() - started) * 1000.0
                with self._lock:
                    self._processed += 1
                    self._last_inference_ms = elapsed_ms
                    self._max_inference_ms = max(self._max_inference_ms, elapsed_ms)
                for score in scores:
                    with self._lock:
                        self._last_score = score
                    consecutive = consecutive + 1 if score >= self.threshold else 0
                    now = time.monotonic()
                    if consecutive >= self.patience and now >= refractory_until:
                        self._events.put((now, score))
                        with self._lock:
                            self._detections += 1
                        consecutive = 0
                        refractory_until = now + 1.0
            except Exception as exc:  # pylint: disable=broad-except
                consecutive = 0
                with self._lock:
                    self._errors += 1
                    self._last_error = str(exc)[:240]
                _LOGGER.exception("ONNX OWW worker failed")
                self._engine.reset()


def runtime_assets_ready() -> bool:
    runtime = (_BRIDGE_PATH.is_file() and _RUNTIME_PATH.is_file()) or importlib.util.find_spec("onnxruntime") is not None
    return bool(runtime and all(path.is_file() for path in (_MELSPEC_PATH, _EMBEDDING_PATH)))
