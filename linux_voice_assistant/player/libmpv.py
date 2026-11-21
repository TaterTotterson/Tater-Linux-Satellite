import logging
import threading
from typing import Any, Callable, Dict, Optional

import mpv

from linux_voice_assistant.player.base import AudioPlayer
from linux_voice_assistant.player.state import PlayerState

_TATER_CHANNEL_FILTERS = {
    "stereo": "",
    "left": "lavfi=[pan=stereo|c0=c0|c1=c0]",
    "right": "lavfi=[pan=stereo|c0=c1|c1=c1]",
    "mono": "lavfi=[pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1]",
}


class LibMpvPlayer(AudioPlayer):
    """
    AudioPlayer implementation for Linux Voice Assistant using libmpv.

    Responsibilities:
    - mpv lifecycle and playback control
    - thread-safe state management
    - volume handling with ducking support
    """

    def __init__(self, device: Optional[str] = None) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._state: PlayerState = PlayerState.IDLE
        self._state_lock = threading.Lock()

        # Volume handling
        self._user_volume: float = 100.0  # 0.0 – 100.0
        self._duck_factor: float = 1.0  # 0.0 – 1.0

        # mpv setup
        self._mpv = mpv.MPV(
            audio_display=False,
            log_handler=self._on_mpv_log,
            loglevel="error",
            cache="yes",
            demuxer_max_bytes="32MiB",
            cache_secs="20",
        )

        if device:
            self._mpv["audio-device"] = device

        # Pre-buffer audio before the sink starts clocking samples out.
        # The default (0.2 s) is too tight for short notification sounds on
        # PulseAudio/PipeWire: the sink stream takes a few ms to initialise
        # and the very first samples are dropped before it is ready, making
        # short files (<1 s) appear to start mid-way through.
        # 0.8 s gives the output pipeline enough headroom without adding any
        # noticeable latency for a user-facing notification sound.
        self._mpv["audio-buffer"] = 0.8

        # Keep the PulseAudio/PipeWire stream open between files by outputting
        # silence when idle.  This eliminates the per-play sink re-initialisation
        # penalty entirely, so back-to-back short sounds (wakeup → TTS, mute →
        # unmute) never lose their first samples regardless of system load.
        self._mpv["audio-stream-silence"] = True

        # Callback Handling
        self._done_callback: Optional[Callable[[], None]] = None
        self._mpv.event_callback("end-file")(self._on_end_file)
        self._mpv.event_callback("start-file")(self._on_start_file)

    # -------- Playback control --------

    def play(
        self,
        url: str,
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = True,
    ) -> None:
        """
        Start playback of a media URL.

        Args:
            url: Media URL or local file path.
            done_callback: Optional callback invoked when playback finishes.
            stop_first: If True, start playback in paused state.
        """
        with self._state_lock:
            self._log.debug("play: current_state=%s", self._state)
            self._done_callback = done_callback
            self._set_state(PlayerState.LOADING)
        self._mpv.pause = stop_first
        self._mpv.play(url)

    def configure_persistent_media(self, *, start_position_ms: int = 0, loop: bool = False) -> None:
        """Configure the next long-form media item without affecting TTS players."""
        with self._state_lock:
            self._mpv["start"] = max(0.0, float(start_position_ms or 0) / 1000.0)
            self._mpv["loop-file"] = "inf" if loop else "no"

    def prepare_synchronized(
        self,
        url: str,
        *,
        channel: str = "stereo",
        loop: bool = False,
        done_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """Load media paused with deterministic channel routing."""
        normalized_channel = str(channel or "stereo").strip().lower()
        audio_filter = _TATER_CHANNEL_FILTERS.get(
            normalized_channel,
            _TATER_CHANNEL_FILTERS["stereo"],
        )
        with self._state_lock:
            self._done_callback = done_callback
            self._set_state(PlayerState.LOADING)
        self._mpv.pause = True
        self._mpv.speed = 1.0
        self._mpv.audio_pitch_correction = False
        self._mpv.loop_file = "inf" if loop else "no"
        self._mpv.command("af", "set", audio_filter)
        self._mpv.play(url)

    def _mpv_property(self, name: str, default: Any = None) -> Any:
        try:
            value = getattr(self._mpv, name)
        except Exception:  # libmpv properties may be unavailable while loading
            return default
        return default if value is None else value

    def synchronized_snapshot(self) -> Dict[str, Any]:
        """Read timeline and cache properties as one thread-safe snapshot."""
        with self._state_lock:
            timeline_position = self._mpv_property("time_pos")
            rendered_position = self._mpv_property("audio_pts")
            position = rendered_position if rendered_position is not None else timeline_position
            raw_cache_duration = self._mpv_property("demuxer_cache_duration")
            cache_duration = max(0.0, float(raw_cache_duration or 0.0))
            duration = self._mpv_property("duration", 0.0)
            return {
                "loaded": position is not None,
                "position_seconds": max(0.0, float(position or 0.0)),
                "timeline_position_seconds": max(0.0, float(timeline_position or 0.0)),
                "rendered_position_seconds": max(0.0, float(position or 0.0)),
                "buffered_seconds": cache_duration,
                # mpv commonly leaves this at zero for a fully downloaded
                # short HTTP WAV. Treat zero as unavailable so firmware can
                # use its stable loaded+paused fallback instead of deadlocking.
                "buffered_seconds_known": bool(raw_cache_duration is not None and cache_duration > 0.0),
                "duration_seconds": max(0.0, float(duration or 0.0)),
                "seeking": bool(self._mpv_property("seeking", False)),
                "rebuffering": bool(self._mpv_property("paused_for_cache", False)),
                "paused": bool(self._mpv_property("pause", False)),
                "speed": float(self._mpv_property("speed", 1.0) or 1.0),
            }

    def seek_synchronized(self, position_seconds: float) -> None:
        """Seek to an absolute media position while retaining pause state."""
        with self._state_lock:
            self._mpv.command(
                "seek",
                max(0.0, float(position_seconds)),
                "absolute+exact",
            )

    def jump_synchronized(self, delta_seconds: float) -> None:
        """Queue a relative exact seek without blocking synchronized control."""
        with self._state_lock:
            self._mpv.command_async(
                "seek",
                float(delta_seconds),
                "relative+exact",
            )

    def set_synchronized_speed(self, speed: float) -> None:
        """Set a narrowly clamped rate for transparent clock correction."""
        with self._state_lock:
            self._mpv.speed = max(0.995, min(1.005, float(speed)))

    def resume_synchronized(self) -> None:
        """Queue pause=false so the scheduler never blocks on MPV."""
        with self._state_lock:
            self._mpv.command_async("set_property", "pause", False)
            self._set_state(PlayerState.PLAYING)

    def reset_synchronized(self) -> None:
        """Restore defaults so ordinary local playback is unaffected."""
        with self._state_lock:
            self._mpv.speed = 1.0
            self._mpv.audio_pitch_correction = True
            self._mpv.loop_file = "no"
            self._mpv.command("af", "set", "")

    def pause(self) -> None:
        """Pause playback."""
        with self._state_lock:
            self._mpv.pause = True
            self._set_state(PlayerState.PAUSED)

    def resume(self) -> None:
        """Resume playback if paused."""
        self._log.debug("unduck() called")
        with self._state_lock:
            self._mpv.pause = False
            self._set_state(PlayerState.PLAYING)

    def stop(self, for_replacement: bool = False) -> None:
        """
        Stop playback.

        If called for track replacement, clears the callback to prevent
        it from being invoked during the transition.
        """
        self._log.debug("unduck() called")
        with self._state_lock:
            if for_replacement:
                # Clear callback to prevent invocation during track transition
                self._done_callback = None
            self._mpv.stop()

    def state(self) -> PlayerState:
        """Return the current player state."""
        with self._state_lock:
            return self._state

    # -------- Volume / Ducking --------

    def set_volume(self, volume: float) -> None:
        """
        Set user volume.

        Args:
            volume: Volume level (0.0–100.0).
        """
        self._log.debug("unduck() called")
        with self._state_lock:
            self._user_volume = max(0.0, min(100.0, float(volume)))
            self._apply_volume()

    def duck(self, factor: float = 0.5) -> None:
        """
        Reduce volume temporarily by a ducking factor.

        Args:
            factor: Ducking factor (0.0–1.0).
        """
        self._log.debug("unduck() called")
        with self._state_lock:
            self._duck_factor = max(0.0, min(1.0, float(factor)))
            self._apply_volume()

    def unduck(self) -> None:
        """Restore volume to the user-defined level."""
        self._log.debug("unduck() called")
        with self._state_lock:
            self._duck_factor = 1.0
            self._apply_volume()

    # -------- Internal helpers --------

    def _apply_volume(self) -> None:
        """Apply effective volume (user volume × duck factor) to mpv."""
        self._log.debug("unduck() called")
        effective = self._user_volume * self._duck_factor
        self._mpv.volume = max(0.0, min(100.0, effective))

    def _on_end_file(self, event) -> None:
        callback: Optional[Callable[[], None]] = None

        with self._state_lock:
            # mpv events: event.data is a MpvEventEndFile object with a 'reason' attribute
            # The reason is an integer constant (see mpv.END_FILE_REASON_*)
            end_file_data = event.data
            reason = getattr(end_file_data, "reason", -1) if end_file_data else -1

            # mpv END_FILE_REASON constants:
            # 0 = eof (end of file), 1 = stop, 2 = abort, 3 = quit, 4 = error
            is_eof = reason == 0

            self._log.debug(
                "_on_end_file: reason=%s (is_eof=%s), state=%s, has_callback=%s",
                reason,
                is_eof,
                self._state,
                self._done_callback is not None,
            )

            # Only process "eof" (reason=0) events as actual track completion.
            # Other reasons are from track changes, stops, or errors.
            if not is_eof:
                self._log.debug("_on_end_file: ignoring non-eof event (reason=%s)", reason)
                return

            self._set_state(PlayerState.IDLE)
            callback = self._done_callback
            self._done_callback = None

        if callback is not None:
            self._log.debug("_on_end_file: invoking callback")
            try:
                callback()
            except RuntimeError:
                # Callback errors must never break the player
                pass

    def _on_start_file(self, event) -> None:
        """Called when mpv starts playing a file."""
        self._log.debug("unduck() called")
        with self._state_lock:
            self._log.debug("_on_start_file: state=%s", self._state)
            self._set_state(PlayerState.PLAYING)

    def _on_mpv_log(self, level: str, prefix: str, text: str) -> None:
        """
        Handle mpv log messages.

        Error and fatal messages transition the player into ERROR state.
        """
        if level in ("error", "fatal"):
            with self._state_lock:
                self._set_state(PlayerState.ERROR)

    def _set_state(self, new_state: PlayerState) -> None:
        """Update internal player state."""
        self._state = new_state
