"""Local timer engine for Tater-native Linux satellites.

Tater deliberately keeps timer state on the satellite that owns the timer.
Keeping the countdown here means an armed timer continues to run and ring when
the Tater server or network connection is temporarily unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .peripheral_api import LVAEvent

_LOGGER = logging.getLogger(__name__)

MAX_TIMERS = 8
MAX_TIMER_SECONDS = 7 * 24 * 60 * 60


def _integer(
    value: Any,
    default: int = 0,
    *,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    try:
        result = int(round(float(value)))
    except (TypeError, ValueError):
        result = int(default)
    return max(minimum, min(maximum, result))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


@dataclass
class NativeTimer:
    """One monotonic local countdown."""

    timer_id: str
    name: str
    original_duration_ms: int
    deadline: float
    task: Optional[asyncio.Task[None]] = None
    ringing: bool = False

    def public(self) -> dict[str, Any]:
        remaining_ms = 0
        if not self.ringing:
            remaining_ms = max(0, int(round((self.deadline - time.monotonic()) * 1000)))
        total_seconds = int(math.ceil(self.original_duration_ms / 1000.0))
        seconds_left = int(math.ceil(remaining_ms / 1000.0))
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
            # Peripheral clients use the same second-based fields as the
            # existing Linux/ESPHome timer event path.
            "total_seconds": total_seconds,
            "seconds_left": seconds_left,
        }


class NativeTimerManager:
    """Handle the timer portion of Tater's native satellite protocol."""

    def __init__(
        self,
        satellite: Any,
        send_message: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.satellite = satellite
        self.state = satellite.state
        self._send_message = send_message
        self.timers: dict[str, NativeTimer] = {}

    def handle_message(self, body: dict[str, Any]) -> bool:
        """Handle one native command, returning whether it was a timer command."""
        message_type = str(body.get("type") or "").strip()
        message_id = str(body.get("id") or "").strip()
        raw_payload = body.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}

        if message_type in {"timer.start", "timer.arm"}:
            self._start(payload, message_id, replace=message_type == "timer.arm")
            return True
        if message_type in {"timer.list", "timer.status"}:
            rows = [timer.public() for timer in self.timers.values()]
            self._result(message_id, "list", timers=rows, count=len(rows))
            return True
        if message_type in {"timer.cancel", "timer.clear"}:
            self._cancel(payload, message_id, clear_all=message_type == "timer.clear")
            return True
        if message_type == "timer.snooze":
            self._snooze(payload, message_id)
            return True
        if message_type == "timer.alarm":
            selected, _ambiguous = self._select(payload)
            for timer in selected:
                self._ring(timer)
            return True
        return False

    def status(self) -> dict[str, Any]:
        """Return heartbeat telemetry and notice locally stopped alarms."""
        self._reconcile_stopped_ringing()
        rows = [timer.public() for timer in self.timers.values()]
        ringing_count = sum(1 for timer in self.timers.values() if timer.ringing)
        return {
            "timer_count": len(rows),
            "timer_ringing": ringing_count > 0,
            "timer": {
                "active": bool(rows),
                "ringing": ringing_count > 0,
                "count": len(rows),
                "ringing_count": ringing_count,
                "timers": rows,
            },
        }

    def connected(self) -> None:
        """Restore an alarm that remained active across a reconnect."""
        self.restore_ringing()

    def restore_ringing(self) -> None:
        """Resume local ringing after the voice transport resets playback."""
        ringing = [timer for timer in self.timers.values() if timer.ringing]
        if not ringing or bool(getattr(self.satellite, "_timer_finished", False)):
            return
        self._start_alarm_playback(ringing[0])

    async def close(self) -> None:
        """Cancel local countdown tasks during application shutdown."""
        tasks = []
        for timer in self.timers.values():
            if timer.task is not None and not timer.task.done():
                timer.task.cancel()
                tasks.append(timer.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.timers.clear()
        self._stop_alarm_playback(emit_idle=False)

    def _result(
        self,
        reply_to: str,
        action: str,
        *,
        ok: bool = True,
        **extra: Any,
    ) -> None:
        self._send_message(
            "timer.result",
            {
                "reply_to": reply_to,
                "action": action,
                "ok": bool(ok),
                **extra,
            },
        )

    def _start(self, payload: dict[str, Any], reply_to: str, *, replace: bool) -> None:
        duration_ms = _integer(
            payload.get("remaining_ms") or payload.get("duration_ms") or (_integer(payload.get("remaining_s") or payload.get("duration_s")) * 1000),
            maximum=MAX_TIMER_SECONDS * 1000,
        )
        original_duration_ms = _integer(
            payload.get("original_duration_ms") or (_integer(payload.get("original_duration_s")) * 1000) or duration_ms,
            maximum=MAX_TIMER_SECONDS * 1000,
        )
        timer_id = str(payload.get("id") or payload.get("timer_id") or uuid.uuid4().hex[:12]).strip()[:47]
        name = str(payload.get("name") or payload.get("label") or "").strip()[:63]

        if duration_ms <= 0:
            self._result(
                reply_to,
                "start",
                ok=False,
                code="invalid_duration",
                message="Timer duration must be greater than zero.",
            )
            return

        existing = self.timers.get(timer_id)
        if existing is not None and not replace:
            self._result(
                reply_to,
                "start",
                timer=existing.public(),
                code="already_exists",
            )
            return
        if existing is None and len(self.timers) >= MAX_TIMERS:
            self._result(
                reply_to,
                "start",
                ok=False,
                code="timer_limit",
                message="This satellite already has the maximum number of timers.",
            )
            return

        was_ringing = bool(existing is not None and existing.ringing)
        if existing is not None and existing.task is not None:
            existing.task.cancel()
        timer = NativeTimer(
            timer_id=timer_id,
            name=name or (existing.name if existing is not None else ""),
            original_duration_ms=original_duration_ms,
            deadline=time.monotonic() + (duration_ms / 1000.0),
        )
        timer.task = asyncio.create_task(self._wait(timer))
        self.timers[timer_id] = timer
        if was_ringing:
            self._refresh_alarm_playback()

        event = "updated" if replace and existing is not None else "armed"
        self._result(reply_to, "start", timer=timer.public())
        self._emit_event(event, timer)
        self.satellite._emit(  # pylint: disable=protected-access
            LVAEvent.TIMER_UPDATED if event == "updated" else LVAEvent.TIMER_TICKING,
            timer.public(),
        )

    async def _wait(self, timer: NativeTimer) -> None:
        try:
            await asyncio.sleep(max(0.0, timer.deadline - time.monotonic()))
        except asyncio.CancelledError:
            return
        if self.timers.get(timer.timer_id) is timer:
            self._ring(timer)

    def _ring(self, timer: NativeTimer) -> None:
        if timer.ringing:
            return
        timer.ringing = True
        timer.deadline = time.monotonic()
        self._start_alarm_playback(timer)
        self._emit_event("expired", timer)

    def _start_alarm_playback(self, timer: NativeTimer) -> None:
        self.satellite._timer_finished = True  # pylint: disable=protected-access
        self.satellite._timer_ring_start = time.monotonic()  # pylint: disable=protected-access
        active_wake_words = getattr(self.state, "active_wake_words", None)
        stop_word = getattr(getattr(self.state, "stop_word", None), "id", None)
        if active_wake_words is not None and stop_word:
            active_wake_words.add(stop_word)
        duck = getattr(self.satellite, "duck", None)
        if callable(duck):
            duck()
        self.satellite._emit(LVAEvent.TIMER_RINGING, timer.public())  # pylint: disable=protected-access
        play_timer = getattr(self.satellite, "_play_timer_finished", None)
        if callable(play_timer):
            play_timer()

    def _select(
        self,
        payload: dict[str, Any],
        *,
        clear_all: bool = False,
    ) -> tuple[list[NativeTimer], bool]:
        timers = list(self.timers.values())
        if clear_all or _truthy(payload.get("all")):
            return timers, False

        ids = {str(value).strip() for value in payload.get("ids", []) if str(value).strip()} if isinstance(payload.get("ids"), list) else set()
        timer_id = str(payload.get("id") or payload.get("timer_id") or "").strip()
        name = str(payload.get("name") or payload.get("label") or "").strip().lower()
        duration_ms = _integer(payload.get("original_duration_ms") or (_integer(payload.get("original_duration_s") or payload.get("duration_s")) * 1000))
        has_criteria = bool(ids or timer_id or name or duration_ms)
        if has_criteria:
            selected = timers
            if ids:
                selected = [timer for timer in selected if timer.timer_id in ids]
            if timer_id:
                selected = [timer for timer in selected if timer.timer_id == timer_id]
            if name:
                selected = [timer for timer in selected if timer.name.lower() == name]
            if duration_ms:
                selected = [timer for timer in selected if timer.original_duration_ms == duration_ms]
            return selected, bool(len(selected) > 1 and not ids)

        ringing = [timer for timer in timers if timer.ringing]
        if ringing:
            return ringing, False
        if len(timers) == 1:
            return timers, False
        return [], len(timers) > 1

    def _cancel(self, payload: dict[str, Any], reply_to: str, *, clear_all: bool) -> None:
        selected, ambiguous = self._select(payload, clear_all=clear_all)
        if ambiguous:
            selected = []
        rows = [timer.public() for timer in selected]
        for timer in selected:
            if timer.task is not None:
                timer.task.cancel()
            self.timers.pop(timer.timer_id, None)
            self._emit_event("cleared" if clear_all else "cancelled", timer, active=False)
        self._refresh_alarm_playback()
        code = "ambiguous" if ambiguous else ("" if selected else "not_found")
        message = "More than one timer is running; specify a timer name or duration." if ambiguous else ("No matching timer is running." if not selected else "")
        self._result(
            reply_to,
            "cancel",
            timers=rows,
            affected=len(rows),
            code=code,
            message=message,
        )

    def _snooze(self, payload: dict[str, Any], reply_to: str) -> None:
        duration_ms = _integer(
            payload.get("duration_ms") or (_integer(payload.get("duration_s"), 300) * 1000),
            300000,
            minimum=1,
            maximum=MAX_TIMER_SECONDS * 1000,
        )
        selected, ambiguous = self._select(payload)
        if ambiguous:
            selected = []
        for timer in selected:
            if timer.task is not None:
                timer.task.cancel()
            timer.ringing = False
            timer.original_duration_ms = duration_ms
            timer.deadline = time.monotonic() + (duration_ms / 1000.0)
            timer.task = asyncio.create_task(self._wait(timer))
            self._emit_event("snoozed", timer)
            self.satellite._emit(LVAEvent.TIMER_UPDATED, timer.public())  # pylint: disable=protected-access
        self._refresh_alarm_playback()
        code = "ambiguous" if ambiguous else ("" if selected else "not_found")
        message = "More than one timer is running; specify a timer name or duration." if ambiguous else ("No matching timer is running." if not selected else "")
        self._result(
            reply_to,
            "snooze",
            timers=[timer.public() for timer in selected],
            affected=len(selected),
            code=code,
            message=message,
        )

    def _emit_event(self, event: str, timer: NativeTimer, *, active: bool = True) -> None:
        row = timer.public()
        row.update(
            {
                "event": event,
                "active": bool(active),
                "state": row["state"] if active else "stopped",
            }
        )
        self._send_message("timer.event", row)

    def _reconcile_stopped_ringing(self) -> None:
        if bool(getattr(self.satellite, "_timer_finished", False)):
            return
        stopped = [timer for timer in self.timers.values() if timer.ringing]
        for timer in stopped:
            self.timers.pop(timer.timer_id, None)
            self._emit_event("stopped", timer, active=False)

    def _refresh_alarm_playback(self) -> None:
        if any(timer.ringing for timer in self.timers.values()):
            return
        self._stop_alarm_playback(emit_idle=True)
        remaining = [timer for timer in self.timers.values() if not timer.ringing]
        if remaining:
            next_timer = min(remaining, key=lambda timer: timer.deadline)
            self.satellite._emit(LVAEvent.TIMER_UPDATED, next_timer.public())  # pylint: disable=protected-access

    def _stop_alarm_playback(self, *, emit_idle: bool) -> None:
        was_ringing = bool(getattr(self.satellite, "_timer_finished", False))
        self.satellite._timer_finished = False  # pylint: disable=protected-access
        self.satellite._timer_ring_start = None  # pylint: disable=protected-access
        active_wake_words = getattr(self.state, "active_wake_words", None)
        stop_word = getattr(getattr(self.state, "stop_word", None), "id", None)
        if active_wake_words is not None and stop_word:
            active_wake_words.discard(stop_word)
        if was_ringing:
            player = getattr(self.state, "tts_player", None)
            stop = getattr(player, "stop", None)
            if callable(stop):
                stop()
            unduck = getattr(self.satellite, "unduck", None)
            if callable(unduck):
                unduck()
        if emit_idle and not self.timers:
            self.satellite._emit(LVAEvent.IDLE, None)  # pylint: disable=protected-access
