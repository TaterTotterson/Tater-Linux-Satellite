import asyncio
import unittest
from types import SimpleNamespace

from linux_voice_assistant.native_timers import NativeTimerManager
from linux_voice_assistant.peripheral_api import LVAEvent

# Tests intentionally inspect the protocol's private timer state.
# pylint: disable=protected-access


class _Player:
    def __init__(self) -> None:
        self.stop_count = 0

    def stop(self) -> None:
        self.stop_count += 1


class _State:
    def __init__(self) -> None:
        self.active_wake_words = set()
        self.stop_word = SimpleNamespace(id="stop")
        self.tts_player = _Player()


class _Satellite:
    def __init__(self) -> None:
        self.state = _State()
        self._timer_finished = False
        self._timer_ring_start = None
        self.emitted = []
        self.duck_count = 0
        self.unduck_count = 0
        self.alarm_count = 0

    def _emit(self, event, data=None) -> None:
        self.emitted.append((event, data))

    def duck(self) -> None:
        self.duck_count += 1

    def unduck(self) -> None:
        self.unduck_count += 1

    def _play_timer_finished(self) -> None:
        self.alarm_count += 1


class NativeTimerManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.satellite = _Satellite()
        self.messages = []
        self.manager = NativeTimerManager(
            self.satellite,
            lambda message_type, payload: self.messages.append({"type": message_type, "payload": payload}),
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close()

    def _command(self, message_type: str, payload=None, message_id="request") -> None:
        handled = self.manager.handle_message(
            {
                "type": message_type,
                "id": message_id,
                "payload": payload or {},
            }
        )
        self.assertTrue(handled)

    def _results(self):
        return [message for message in self.messages if message["type"] == "timer.result"]

    def _events(self):
        return [message for message in self.messages if message["type"] == "timer.event"]

    async def test_start_list_and_cancel_round_trip(self) -> None:
        self._command(
            "timer.start",
            {"id": "tea", "name": "Tea", "duration_ms": 60_000},
            "start-request",
        )

        started = self._results()[-1]["payload"]
        self.assertTrue(started["ok"])
        self.assertEqual(started["reply_to"], "start-request")
        self.assertEqual(started["timer"]["id"], "tea")
        self.assertEqual(started["timer"]["total_seconds"], 60)
        self.assertGreater(started["timer"]["seconds_left"], 0)
        self.assertEqual(self._events()[-1]["payload"]["event"], "armed")
        self.assertEqual(self.satellite.emitted[-1][0], LVAEvent.TIMER_TICKING)

        self._command("timer.list", message_id="list-request")
        listed = self._results()[-1]["payload"]
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["timers"][0]["name"], "Tea")

        self._command("timer.cancel", {"id": "tea"}, "cancel-request")
        cancelled = self._results()[-1]["payload"]
        self.assertEqual(cancelled["affected"], 1)
        self.assertFalse(self.manager.timers)
        self.assertEqual(self.satellite.emitted[-1][0], LVAEvent.IDLE)

    async def test_countdown_rings_locally_and_reports_expiration(self) -> None:
        self._command(
            "timer.start",
            {"id": "short", "name": "Short", "duration_ms": 20},
        )
        await asyncio.sleep(0.06)

        timer = self.manager.timers["short"]
        self.assertTrue(timer.ringing)
        self.assertTrue(self.satellite._timer_finished)
        self.assertEqual(self.satellite.alarm_count, 1)
        self.assertEqual(self.satellite.duck_count, 1)
        self.assertIn("stop", self.satellite.state.active_wake_words)
        self.assertEqual(self.satellite.emitted[-1][0], LVAEvent.TIMER_RINGING)
        self.assertEqual(self._events()[-1]["payload"]["event"], "expired")

    async def test_snooze_stops_alarm_and_rearms_countdown(self) -> None:
        self._command("timer.start", {"id": "nap", "duration_ms": 10})
        await asyncio.sleep(0.04)
        self.assertTrue(self.manager.timers["nap"].ringing)

        self._command("timer.snooze", {"id": "nap", "duration_ms": 100})

        timer = self.manager.timers["nap"]
        self.assertFalse(timer.ringing)
        self.assertFalse(self.satellite._timer_finished)
        self.assertEqual(self.satellite.state.tts_player.stop_count, 1)
        self.assertEqual(self.satellite.unduck_count, 1)
        self.assertEqual(self.satellite.emitted[-1][0], LVAEvent.TIMER_UPDATED)
        self.assertEqual(self._results()[-1]["payload"]["affected"], 1)

    async def test_ambiguous_cancel_preserves_multiple_timers(self) -> None:
        self._command("timer.start", {"id": "one", "duration_ms": 30_000})
        self._command("timer.start", {"id": "two", "duration_ms": 60_000})

        self._command("timer.cancel", {})

        result = self._results()[-1]["payload"]
        self.assertEqual(result["code"], "ambiguous")
        self.assertEqual(result["affected"], 0)
        self.assertEqual(set(self.manager.timers), {"one", "two"})

    async def test_local_stop_word_is_reconciled_into_timer_event(self) -> None:
        self._command("timer.start", {"id": "stop-me", "duration_ms": 10})
        await asyncio.sleep(0.04)
        self.satellite._timer_finished = False

        status = self.manager.status()

        self.assertEqual(status["timer_count"], 0)
        self.assertNotIn("stop-me", self.manager.timers)
        self.assertEqual(self._events()[-1]["payload"]["event"], "stopped")
        self.assertFalse(self._events()[-1]["payload"]["active"])

    async def test_ringing_is_restored_after_transport_resets_playback(self) -> None:
        self._command("timer.start", {"id": "offline", "duration_ms": 10})
        await asyncio.sleep(0.04)
        self.assertEqual(self.satellite.alarm_count, 1)

        self.satellite._timer_finished = False
        self.manager.restore_ringing()

        self.assertIn("offline", self.manager.timers)
        self.assertTrue(self.satellite._timer_finished)
        self.assertEqual(self.satellite.alarm_count, 2)


if __name__ == "__main__":
    unittest.main()
