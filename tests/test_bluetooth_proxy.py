import asyncio
import base64
import importlib.util
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "linux_voice_assistant/bluetooth_proxy.py"
if not MODULE_PATH.exists():
    MODULE_PATH = ROOT / "buildroot/package/thirdreality/tater-linux-satellite/files/bluetooth_proxy.py"
SPEC = importlib.util.spec_from_file_location("demand_proxy", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class Backend:
    def __init__(self):
        self.links = []
        self.powers = []
        self.scans = []
        self.pair_calls = []
        self.writes = []
        self.block = None

    def connected(self):
        return list(self.links)

    async def power(self, enabled):
        self.powers.append(enabled)

    async def scan(self, enabled):
        self.scans.append(enabled)

    async def connect(self, addr, kind):
        if self.block:
            await self.block.wait()
        self.links.append(addr)

    async def disconnect(self, addr):
        if addr in self.links:
            self.links.remove(addr)

    async def forget(self, addr):
        await self.disconnect(addr)

    async def pair(self, addr, pin):
        self.pair_calls.append((addr, pin))
        return {"bonded": True, "encrypted": True, "authenticated": True}

    async def services(self, addr):
        return [{"uuid": "service", "start": 1, "end": 7, "chars": []}]

    async def read(self, addr, handle):
        return b"abc"

    async def write(self, addr, handle, value, response):
        self.writes.append((handle, value, response))


class DemandProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        self.backend = Backend()
        self.busy = False
        self.proxy = module.BluetoothProxy(lambda kind, body: self.sent.append((kind, body)), backend=self.backend, should_pause=lambda: self.busy)
        self.addAsyncCleanup(self.proxy.close)
        self.counter = 0

    async def call(self, action, **payload):
        self.counter += 1
        message_id = str(self.counter)
        self.assertTrue(self.proxy.handle_message({"type": "ble.gatt", "id": message_id, "payload": {"t": action, "req": self.counter, **payload}}))
        await asyncio.gather(*tuple(self.proxy.tasks))
        return next(body for kind, body in self.sent if kind == "ble.gatt.result" and body["reply_to"] == message_id)

    async def test_status_and_slots_never_start_bluetooth(self):
        self.assertFalse(self.proxy.status()["enabled"])
        response = await self.call("slots")
        self.assertTrue(response["ok"])
        self.assertEqual(response["limit"], 2)
        self.assertEqual(self.backend.powers, [])
        self.assertEqual(self.backend.scans, [])

    async def test_discovery_expires_and_releases_radio(self):
        self.assertTrue((await self.call("scan", duration_s=5))["ok"])
        self.assertEqual(self.backend.powers, [True])
        self.assertEqual(self.backend.scans, [True])
        self.proxy.until = time.monotonic() - 1
        await asyncio.sleep(0.3)
        self.assertEqual(self.backend.scans, [True, False])
        self.proxy._idle = time.monotonic() - 4
        await asyncio.sleep(0.3)
        self.assertEqual(self.backend.powers, [True, False])

    async def test_stopping_scan_preserves_a_device_in_use(self):
        addr = "aa:bb:cc:dd:ee:ff"
        await self.call("connect", addr=addr)
        await self.call("scan", duration_s=5)
        await self.call("stop_scan")
        self.proxy._idle = time.monotonic() - 4
        await asyncio.sleep(0.3)
        self.assertEqual(self.backend.links, [addr])
        self.assertEqual(self.backend.powers, [True])
        self.assertFalse(self.proxy.scanning)

    async def test_voice_pauses_discovery_without_disconnecting(self):
        await self.call("scan", duration_s=5)
        self.busy = True
        await asyncio.sleep(0.3)
        self.assertFalse(self.proxy.scanning)
        self.proxy.until = time.monotonic() - 1
        self.busy = False
        await asyncio.sleep(0.3)
        self.assertFalse(self.proxy.scanning)
        self.assertEqual(self.backend.scans, [True, False])

    async def test_connection_pairing_read_write_use_common_wire_contract(self):
        addr = "aa:bb:cc:dd:ee:ff"
        await self.call("connect", addr=addr)
        response = await self.call("pair", addr=addr, pin="012345")
        self.assertTrue(response["authenticated"])
        self.assertEqual(self.backend.pair_calls, [(addr, "012345")])
        self.assertEqual((await self.call("read", addr=addr, handle=9))["value"], base64.b64encode(b"abc").decode())
        self.assertTrue((await self.call("write", addr=addr, handle=9, value="YWJj", response=True))["ok"])
        self.assertEqual(self.backend.writes, [(9, b"abc", True)])
        self.assertTrue((await self.call("services", addr=addr))["services"])
        self.assertNotIn("012345", str(self.sent))

    async def test_invalid_requests_cannot_start_radio(self):
        for action, payload in [("scan", {"duration_s": 0}), ("scan", {"duration_s": True}), ("connect", {"addr": "invalid"}), ("erase", {"addr": "aa:bb:cc:dd:ee:ff"})]:
            self.assertFalse((await self.call(action, **payload))["ok"])
        self.assertEqual(self.backend.powers, [])

    async def test_limit_and_bad_pin_preserve_existing_links(self):
        self.backend.links = ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
        self.assertEqual((await self.call("connect", addr="aa:bb:cc:dd:ee:03"))["error"], "no_slots")
        self.assertFalse((await self.call("pair", addr=self.backend.links[0], pin="nope"))["ok"])
        self.assertEqual(len(self.backend.links), 2)
        self.assertEqual(self.backend.pair_calls, [])

    async def test_controller_disconnect_cancels_pending_connections(self):
        self.backend.block = asyncio.Event()
        self.proxy.handle_message({"type": "ble.gatt", "id": "pending", "payload": {"t": "connect", "req": 1, "addr": "aa:bb:cc:dd:ee:ff"}})
        await asyncio.sleep(0.02)
        await self.proxy.close()
        self.assertEqual(self.backend.links, [])
        self.assertFalse(self.proxy.powered)
        self.assertFalse(self.proxy.tasks)

    async def test_stop_waits_for_inflight_discovery_start(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def power(enabled):
            self.backend.powers.append(enabled)
            if enabled:
                entered.set()
                await release.wait()

        self.backend.power = power
        start = asyncio.create_task(self.call("scan", duration_s=15))
        await entered.wait()
        stop = asyncio.create_task(self.call("stop_scan"))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(start, stop)
        self.assertFalse(self.proxy.scanning)
        self.assertEqual(self.proxy.until, 0)
        self.assertEqual(self.backend.scans, [True, False])

    async def test_concurrent_connections_cannot_exceed_capacity(self):
        self.backend.block = asyncio.Event()
        calls = [asyncio.create_task(self.call("connect", addr=f"aa:bb:cc:dd:ee:{number:02x}")) for number in range(3)]
        await asyncio.sleep(0.01)
        self.backend.block.set()
        results = await asyncio.gather(*calls)
        self.assertEqual(len(self.backend.links), 2)
        self.assertEqual(sum(result.get("error") == "no_slots" for result in results), 1)

    async def test_failed_operations_release_address_locks(self):
        response = await self.call("read", addr="aa:bb:cc:dd:ee:ff", handle=1)
        self.assertEqual(response["error"], "not_connected")
        self.assertFalse(self.proxy.locks)

    async def test_disconnect_powers_off_even_when_stopping_scan_fails(self):
        await self.call("scan", duration_s=5)

        async def stop_failure(enabled):
            if not enabled:
                raise RuntimeError("adapter error")

        self.backend.scan = stop_failure
        await self.proxy.close()
        self.assertEqual(self.backend.powers, [True, False])
        self.assertFalse(self.proxy.scanning)


if __name__ == "__main__":
    unittest.main()
