"""Linux BlueZ backend for the common satellite Bluetooth proxy protocol."""

import asyncio
import base64
import contextlib
import json
import os
import tempfile
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .bluez_agent import PairingAgent

CCCD = "00002902-0000-1000-8000-00805f9b34fb"
PROP = "org.freedesktop.DBus.Properties"
DEVICE = "org.bluez.Device1"
ADAPTER = "org.bluez.Adapter1"
AGENT = "/com/tater/bluetooth/agent"


class BlueZBackend:
    def __init__(self, send, event):
        self.send, self.event = send, event
        self.bus = None
        self.adapter = ""
        self.agent = PairingAgent()
        self.pair_lock = asyncio.Lock()
        self.clients = {}
        self.paths = {}
        self.attributes = {}
        self.notifications = set()
        self.scanner = None
        self.bond_path = Path(os.environ.get("TATER_BLE_BONDS_PATH") or Path.home() / ".local/share/tater/bluetooth_bonds.json")
        self.authenticated = set()
        self._bonds_loaded = False

    async def call(self, path, interface, member, signature="", body=None):
        reply = await asyncio.wait_for(self.bus.call(Message(destination="org.bluez", path=path, interface=interface, member=member, signature=signature, body=body or [])), 30)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(reply.error_name)
        return reply.body

    async def objects(self):
        return (await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]

    async def _init(self):
        if self.bus is not None:
            return
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus = bus
        try:
            objects = await self.objects()
            self.adapter = next(path for path, interfaces in objects.items() if ADAPTER in interfaces)
            self.bus.export(AGENT, self.agent)
            await self.call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", [AGENT, "KeyboardOnly"])
            if not self._bonds_loaded:
                try:
                    rows = json.loads(self.bond_path.read_text(encoding="utf-8"))
                    self.authenticated = set(rows) if isinstance(rows, list) else set()
                except FileNotFoundError:
                    pass
                self._bonds_loaded = True
        except BaseException:
            self.bus = None
            bus.disconnect()
            raise

    async def _controller(self, action):
        setup = Path("/etc/init.d/S44tater-bluetooth")
        if not setup.exists():
            return False
        process = await asyncio.create_subprocess_exec(str(setup), action, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            code = await asyncio.wait_for(process.wait(), 12)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if code:
            raise RuntimeError("Bluetooth controller state change failed")
        return True

    async def power(self, enabled):
        if not enabled:
            error = None
            if self.bus is not None:
                try:
                    await self.scan(False)
                except Exception as exc:
                    error = exc
                try:
                    await self.call(self.adapter, PROP, "Set", "ssv", [ADAPTER, "Powered", Variant("b", False)])
                except Exception as exc:
                    error = exc
            disabled = await self._controller("disable")
            if error is not None and not disabled:
                raise error
            return
        await self._controller("enable")
        if self.scanner is not None:
            await self.scan(False)
        deadline = asyncio.get_running_loop().time() + 8
        while True:
            try:
                await self._init()
                objects = await self.objects()
                self.adapter = next(path for path, interfaces in objects.items() if ADAPTER in interfaces)
                break
            except (RuntimeError, StopIteration) as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("Bluetooth adapter did not become ready") from exc
                await asyncio.sleep(0.2)
        await self.call(self.adapter, PROP, "Set", "ssv", [ADAPTER, "Powered", Variant("b", True)])

    def connected(self):
        return [addr for addr, client in self.clients.items() if client.is_connected]

    async def scan(self, enabled):
        if enabled and self.scanner is None:
            scanner = BleakScanner(detection_callback=self._advertisement, adapter=self.adapter.rsplit("/", 1)[-1])
            await scanner.start()
            self.scanner = scanner
        elif not enabled and self.scanner is not None:
            await self.scanner.stop()
            self.scanner = None

    def _advertisement(self, device, advertisement):
        # Only a bounded, user-requested discovery session starts this scanner.
        self.send(
            "ble.advertisements",
            {
                "adverts": [
                    {
                        "address": device.address.lower(),
                        "address_type": 1 if device.details.get("props", {}).get("AddressType") == "random" else 0,
                        "name": advertisement.local_name or device.name or "",
                        "rssi": advertisement.rssi,
                        "service_uuids": list(advertisement.service_uuids or [])[:64],
                        "manufacturer_data": {str(key): bytes(value)[:252].hex() for key, value in list(advertisement.manufacturer_data.items())[:16]},
                        "service_data": {key: bytes(value)[:238].hex() for key, value in list(advertisement.service_data.items())[:16]},
                        "data": "",
                    }
                ]
            },
        )

    async def _device(self, address, discover=True):
        async def lookup():
            for path, interfaces in (await self.objects()).items():
                values = interfaces.get(DEVICE, {})
                if str(getattr(values.get("Address"), "value", "")).lower() == address:
                    props = {key: value.value for key, value in values.items()}
                    return BLEDevice(address, props.get("Name") or address, {"path": path, "props": props})
            return None

        found = await lookup()
        if found or not discover:
            return found
        # Reconnection can need a short discovery even when the room tracker is gone.
        owned = self.scanner is None
        if owned:
            await self.scan(True)
        try:
            for _ in range(40):
                found = await lookup()
                if found:
                    return found
                await asyncio.sleep(0.25)
            raise RuntimeError("Bluetooth device was not found")
        finally:
            if owned:
                await self.scan(False)

    async def connect(self, address, _address_type):
        device = await self._device(address)
        client = BleakClient(device, disconnected_callback=lambda value: self._disconnected(address), timeout=25)
        try:
            await client.connect()
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        self.clients[address] = client
        self.paths[address] = device.details["path"]

    def _disconnected(self, address):
        existed = self.clients.pop(address, None)
        self.paths.pop(address, None)
        self.attributes.pop(address, None)
        self.notifications = {key for key in self.notifications if key[0] != address}
        if existed is not None:
            self.event({"t": "disconnected", "addr": address, "reason": 19})

    async def disconnect(self, address):
        client = self.clients.get(address)
        if client:
            await client.disconnect()
            self._disconnected(address)

    def _save_authenticated(self):
        self.bond_path.parent.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".ble-bonds-", dir=self.bond_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(sorted(self.authenticated), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.bond_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    async def pair(self, address, pin):
        path = self.paths[address]
        async with self.pair_lock:
            paired = (await self.call(path, PROP, "Get", "ss", [DEVICE, "Paired"]))[0].value
            if paired and address not in self.authenticated:
                raise RuntimeError("Remove the existing unauthenticated bond before pairing")
            if not paired:
                self.agent.device, self.agent.pin, self.agent.used = path, pin, False
                try:
                    await self.call(path, DEVICE, "Pair")
                    used = self.agent.used
                finally:
                    self.agent.device = self.agent.pin = ""
                if not used:
                    await self.call(self.adapter, ADAPTER, "RemoveDevice", "o", [path])
                    raise RuntimeError("Device did not use authenticated PIN pairing")
                self.authenticated.add(address)
                try:
                    self._save_authenticated()
                except BaseException:
                    self.authenticated.discard(address)
                    await self.call(self.adapter, ADAPTER, "RemoveDevice", "o", [path])
                    raise
            retained = (await self.call(path, PROP, "Get", "ss", [DEVICE, "Paired"]))[0].value
            if not retained:
                raise RuntimeError("Bluetooth bond was not retained")
            await self.call(path, PROP, "Set", "ssv", [DEVICE, "Trusted", Variant("b", True)])
            return {"bonded": True, "encrypted": True, "authenticated": True}

    async def forget(self, address):
        await self.disconnect(address)
        device = await self._device(address, discover=False)
        if device:
            await self.call(self.adapter, ADAPTER, "RemoveDevice", "o", [device.details["path"]])
        self.authenticated.discard(address)
        self._save_authenticated()

    async def services(self, address):
        client = self.clients[address]
        collection = client.services
        services = sorted(collection, key=lambda service: service.handle)
        attributes = {}
        occupied = {char.handle for svc in services for char in svc.characteristics} | {desc.handle for svc in services for char in svc.characteristics for desc in char.descriptors}
        synthetic = 65534
        result = []
        for index, service in enumerate(services):
            row = {"uuid": service.uuid, "start": service.handle, "end": services[index + 1].handle - 1 if index + 1 < len(services) else 65535, "chars": []}
            for char in service.characteristics:
                flags = {"read": 2, "write-without-response": 4, "write": 8, "notify": 16, "indicate": 32}
                props = sum(value for name, value in flags.items() if name in char.properties)
                attributes[char.handle] = ("char", char)
                descriptors = []
                for desc in char.descriptors:
                    attributes[desc.handle] = ("cccd" if desc.uuid.lower() == CCCD else "desc", char if desc.uuid.lower() == CCCD else desc)
                    descriptors.append({"uuid": desc.uuid, "handle": desc.handle})
                if props & 48 and not any(desc["uuid"].lower() == CCCD for desc in descriptors):
                    while synthetic in occupied:
                        synthetic -= 1
                    occupied.add(synthetic)
                    attributes[synthetic] = ("cccd", char)
                    descriptors.append({"uuid": CCCD, "handle": synthetic})
                    synthetic -= 1
                row["chars"].append({"uuid": char.uuid, "handle": max(1, char.handle - 1), "value_handle": char.handle, "props": props, "descs": descriptors})
            result.append(row)
        self.attributes[address] = attributes
        return result

    async def _attribute(self, address, handle):
        if address not in self.attributes:
            await self.services(address)
        if handle not in self.attributes[address]:
            raise ValueError("Unknown GATT handle")
        return self.attributes[address][handle]

    async def read(self, address, handle):
        kind, attribute = await self._attribute(address, handle)
        if kind == "cccd":
            return b"\x01\x00" if (address, handle) in self.notifications else b"\x00\x00"
        client = self.clients[address]
        return bytes(await (client.read_gatt_char(attribute) if kind == "char" else client.read_gatt_descriptor(handle)))

    async def write(self, address, handle, value, response):
        kind, attribute = await self._attribute(address, handle)
        client = self.clients[address]
        if kind == "cccd":
            if value not in (b"\x00\x00", b"\x01\x00", b"\x02\x00"):
                raise ValueError("Invalid notification configuration")
            key = (address, handle)
            if value == b"\x00\x00":
                if key in self.notifications:
                    await client.stop_notify(attribute)
                    self.notifications.discard(key)
            elif key not in self.notifications:
                await client.start_notify(
                    attribute, lambda _char, data: self.event({"t": "notify", "addr": address, "handle": attribute.handle, "value": base64.b64encode(data).decode(), "ind": value == b"\x02\x00"})
                )
                self.notifications.add(key)
        elif kind == "char":
            await client.write_gatt_char(attribute, value, response=response)
        else:
            await client.write_gatt_descriptor(handle, value)
