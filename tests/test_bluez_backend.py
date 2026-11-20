import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "linux_voice_assistant"
if not DIRECTORY.exists():
    DIRECTORY = ROOT / "buildroot/package/thirdreality/tater-linux-satellite/files"
try:
    import bleak
    import dbus_fast

    package = types.ModuleType("_test_satellite_bluez")
    package.__path__ = [str(DIRECTORY)]
    sys.modules[package.__name__] = package
    module = __import__("_test_satellite_bluez.bluez_backend", fromlist=["BlueZBackend"])
except ImportError:
    module = None


@unittest.skipIf(module is None, "BlueZ backend dependencies are not installed on this host")
class BlueZBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.backend = module.BlueZBackend(lambda *x: None, self.events.append)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend.bond_path = Path(self.temp.name) / "bonds.json"
        self.addr = "aa:bb:cc:dd:ee:ff"
        self.path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
        self.backend.paths[self.addr] = self.path
        self.backend.adapter = "/org/bluez/hci0"

    async def test_notifications_map_to_wire_handles_without_writing_cccd_directly(self):
        char = types.SimpleNamespace(handle=12, uuid="abcd", properties=["read", "notify"], descriptors=[])
        svc = types.SimpleNamespace(handle=1, uuid="service", characteristics=[char])
        client = types.SimpleNamespace(
            services=[svc], is_connected=True, start_notify=AsyncMock(), stop_notify=AsyncMock(), write_gatt_char=AsyncMock(), read_gatt_char=AsyncMock(return_value=b"hello")
        )
        self.backend.clients[self.addr] = client
        services = await self.backend.services(self.addr)
        descriptor = services[0]["chars"][0]["descs"][0]
        self.assertEqual(descriptor["uuid"], module.CCCD)
        await self.backend.write(self.addr, descriptor["handle"], b"\x01\x00", True)
        callback = client.start_notify.await_args.args[1]
        callback(char, b"abc")
        self.assertEqual(self.events[-1], {"t": "notify", "addr": self.addr, "handle": 12, "value": "YWJj", "ind": False})
        await self.backend.write(self.addr, descriptor["handle"], b"\x00\x00", True)
        client.stop_notify.assert_awaited_once_with(char)
        client.write_gatt_char.assert_not_awaited()
        self.assertEqual(await self.backend.read(self.addr, 12), b"hello")

    async def test_pin_agent_is_scoped_to_current_pairing_device(self):
        agent = self.backend.agent
        agent.device = self.path
        agent.pin = "012345"
        with self.assertRaises(Exception):
            agent._pin("/another_device")
        self.assertEqual(agent._pin(self.path), "012345")
        self.assertTrue(agent.used)

    async def test_pair_clears_pin_and_writes_only_authenticated_marker(self):
        paired = False

        async def call(path, interface, member, *args):
            nonlocal paired
            if member == "Get":
                return [types.SimpleNamespace(value=paired)]
            if member == "Pair":
                self.assertEqual(self.backend.agent._pin(path), "012345")
                paired = True
            return []

        self.backend.call = call
        result = await self.backend.pair(self.addr, "012345")
        self.assertTrue(result["authenticated"])
        self.assertEqual(self.backend.agent.pin, "")
        saved = self.backend.bond_path.read_text()
        self.assertNotIn("012345", saved)
        self.assertIn(self.addr, saved)
        self.assertEqual(self.backend.bond_path.stat().st_mode & 0o777, 0o600)

    async def test_just_works_cannot_be_reported_as_authenticated_pin_pairing(self):
        calls = []

        async def call(path, interface, member, *args):
            calls.append(member)
            if member == "Get":
                return [types.SimpleNamespace(value=False)]
            return []

        self.backend.call = call
        with self.assertRaises(RuntimeError):
            await self.backend.pair(self.addr, "012345")
        self.assertIn("RemoveDevice", calls)
        self.assertFalse(self.backend.bond_path.exists())
        self.assertEqual(self.backend.agent.pin, "")

    async def test_forgetting_bond_does_not_start_discovery(self):
        self.backend.disconnect = AsyncMock()
        self.backend._device = AsyncMock(return_value=None)
        self.backend.scan = AsyncMock()
        await self.backend.forget(self.addr)
        self.backend._device.assert_awaited_once_with(self.addr, discover=False)
        self.backend.scan.assert_not_awaited()

    async def test_disconnect_event_emitted_only_once(self):
        self.backend.clients[self.addr] = object()
        self.backend._disconnected(self.addr)
        self.backend._disconnected(self.addr)
        self.assertEqual(len(self.events), 1)

    async def test_failed_scan_stop_can_be_retried(self):
        scanner = types.SimpleNamespace(stop=AsyncMock(side_effect=[RuntimeError("adapter busy"), None]))
        self.backend.scanner = scanner
        with self.assertRaises(RuntimeError):
            await self.backend.scan(False)
        self.assertIs(self.backend.scanner, scanner)
        await self.backend.scan(False)
        self.assertIsNone(self.backend.scanner)
        self.assertEqual(scanner.stop.await_count, 2)

    async def test_power_off_is_attempted_even_if_scan_cleanup_fails(self):
        self.backend.bus = object()
        self.backend.scan = AsyncMock(side_effect=RuntimeError("scan cleanup failed"))
        self.backend.call = AsyncMock(return_value=[])
        self.backend._controller = AsyncMock(return_value=False)
        with self.assertRaises(RuntimeError):
            await self.backend.power(False)
        self.backend.call.assert_awaited_once()
        self.assertEqual(self.backend.call.await_args.args[4][1], "Powered")


if __name__ == "__main__":
    unittest.main()
