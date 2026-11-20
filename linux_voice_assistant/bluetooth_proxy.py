"""On-demand satellite GATT protocol, shared with Echo; no presence observer."""

import asyncio
import base64
import contextlib
import importlib.util
import re
import sys
import time

MAX_LINKS = 2
MAX_PENDING = 24


def normalize_address(value):
    value = str(value or "").lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", value):
        raise ValueError("Invalid Bluetooth address")
    return value


class BluetoothProxy:
    def __init__(self, send, *, backend=None, should_pause=lambda: False):
        self.send = send
        self.backend = backend
        self.should_pause = should_pause
        self.supported = backend is not None or (sys.platform == "linux" and importlib.util.find_spec("bleak") is not None)
        self.tasks = set()
        self.locks = {}
        self.until = 0.0
        self.powered = False
        self.scanning = False
        self._poll = None
        self._power_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._idle = 0.0
        self._closing = False

    def capabilities(self):
        return {
            "ble_gatt": self.supported,
            "ble_gatt_version": 1,
            "ble_gatt_pairing": self.supported,
            "ble_gatt_pairing_version": 1,
            "ble_advertisements": self.supported,
            "ble_advertisements_version": 1,
            "ble_on_demand": self.supported,
            "ble_on_demand_version": 1,
        }

    def status(self):
        return {"supported": self.supported, "enabled": self.powered, "scanning": self.scanning, "connections": len(self.backend.connected()) if self.backend else 0}

    def _event(self, body):
        self.send("ble.gatt.event", body)

    def slots(self):
        addresses = self.backend.connected() if self.backend else []
        return {"t": "slots", "free": max(0, MAX_LINKS - len(addresses)), "limit": MAX_LINKS, "addrs": addresses, "enabled": self.powered, "scanning": self.scanning}

    def handle_message(self, body):
        if body.get("type") != "ble.gatt":
            return False
        payload = body.get("payload") or {}
        request_id = str(body.get("id") or "")
        req = payload.get("req") if isinstance(payload, dict) else None
        if not isinstance(req, int) or isinstance(req, bool) or not 0 <= req <= 0xFFFFFFFF:
            self.send("ble.gatt.result", {"reply_to": request_id, "req": 0, "t": "result", "ok": False, "error": "bad_request"})
            return True
        if len(self.tasks) >= MAX_PENDING or self._closing:
            self.send("ble.gatt.result", {"reply_to": request_id, "req": req, "t": "result", "ok": False, "error": "busy"})
            return True
        task = asyncio.create_task(self._run(request_id, dict(payload)))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def _ensure(self):
        if not self.supported:
            raise RuntimeError("Bluetooth proxy backend is unavailable")
        async with self._power_lock:
            if self.backend is None:
                from .bluez_backend import BlueZBackend

                self.backend = BlueZBackend(self.send, self._event)
            if not self.powered:
                try:
                    await self.backend.power(True)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await self.backend.power(False)
                    raise
                self.powered = True
            self._idle = 0.0
            if self._poll is None or self._poll.done():
                self._poll = asyncio.create_task(self._service())

    async def _set_scan(self, enabled):
        if self.scanning == enabled:
            return
        await self.backend.scan(enabled)
        self.scanning = enabled
        self._event(self.slots())

    async def _service(self):
        while self.powered:
            try:
                async with self._power_lock:
                    desired = time.monotonic() < self.until and not self.should_pause()
                    await self._set_scan(desired)
                    if desired or time.monotonic() < self.until or self.tasks or self.backend.connected():
                        self._idle = 0.0
                    elif not self._idle:
                        self._idle = time.monotonic()
                    elif time.monotonic() - self._idle >= 3:
                        await self.backend.power(False)
                        self.powered = False
                        self._event(self.slots())
                        break
                await asyncio.sleep(0.25)
            except Exception:
                # Retire discovery and retry shutdown after transient errors.
                # Never reset a radio underneath an active device connection.
                self.until = 0
                self._event({"t": "status", "enabled": self.powered, "scanning": self.scanning, "error": "Bluetooth discovery or shutdown needs attention"})
                await asyncio.sleep(1)

    async def _run(self, message_id, payload):
        result = {"reply_to": message_id, "t": "result", "req": payload["req"], "ok": False}
        lock, addr = None, ""
        try:
            action = payload.get("t")
            if action == "slots":
                result.update(self.slots())
            elif action in {"scan", "stop_scan"}:
                # Keep a Stop queued behind an in-flight power-up, so that a
                # slow controller cannot restart discovery after Stop succeeds.
                async with self._control_lock:
                    if action == "scan":
                        duration = payload.get("duration_s", 15)
                        if not isinstance(duration, int) or isinstance(duration, bool) or not 5 <= duration <= 60:
                            raise ValueError("Discovery must last 5–60 seconds")
                        await self._ensure()
                        async with self._power_lock:
                            self.until = time.monotonic() + duration
                            await self._set_scan(not self.should_pause())
                    else:
                        async with self._power_lock:
                            self.until = 0
                            if self.powered:
                                await self._set_scan(False)
            else:
                addr = normalize_address(payload.get("addr"))
                if action not in {"connect", "disconnect", "pair", "forget", "services", "read", "write"}:
                    raise ValueError("Unknown Bluetooth operation")
                lock = self.locks.setdefault(addr, asyncio.Lock())
                async with lock:
                    if action in {"connect", "forget"}:
                        await self._ensure()
                    if action == "connect":
                        async with self._connection_lock:
                            if addr in self.backend.connected():
                                result["error"] = "already_connected"
                            elif len(self.backend.connected()) >= MAX_LINKS:
                                raise RuntimeError("no_slots")
                            else:
                                await self.backend.connect(addr, payload.get("addr_type", 0))
                                result["mtu"] = 23
                    elif action == "disconnect":
                        if self.backend:
                            await self.backend.disconnect(addr)
                    elif action == "forget":
                        await self.backend.forget(addr)
                    else:
                        if not self.backend or addr not in self.backend.connected():
                            raise RuntimeError("not_connected")
                        if action == "pair":
                            pin = str(payload.get("pin") or "")
                            if not re.fullmatch(r"[0-9]{6}", pin):
                                raise ValueError("PIN must contain six digits")
                            result.update(await self.backend.pair(addr, pin))
                        elif action == "services":
                            result["services"] = await self.backend.services(addr)
                        else:
                            handle = payload.get("handle")
                            if not isinstance(handle, int) or isinstance(handle, bool) or not 1 <= handle <= 65535:
                                raise ValueError("Invalid GATT handle")
                            if action == "read":
                                result["value"] = base64.b64encode(await self.backend.read(addr, handle)).decode()
                            else:
                                value = base64.b64decode(payload.get("value") or "", validate=True)
                                if len(value) > 512:
                                    raise ValueError("GATT value exceeds 512 bytes")
                                await self.backend.write(addr, handle, value, bool(payload.get("response")))
            result["ok"] = True
        except asyncio.CancelledError:
            result["error"] = "disconnected"
            raise
        except Exception as exc:
            result["error"] = "bad_request" if isinstance(exc, ValueError) else "failed"
            # PINs and other credential material never enter responses or logs.
            if str(exc) in {"no_slots", "not_connected", "already_connected"}:
                result["error"] = str(exc)
        finally:
            # Retire failed as well as successful address operations. Otherwise
            # repeated failed connections leave an unbounded lock registry.
            if lock is not None and not lock.locked() and not getattr(lock, "_waiters", None):
                self.locks.pop(addr, None)
            self.send("ble.gatt.result", result)
            self._event(self.slots())

    async def close(self):
        self._closing = True
        self.until = 0
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self._poll:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
        async with self._power_lock:
            if self.backend:
                for addr in self.backend.connected():
                    with contextlib.suppress(Exception):
                        await self.backend.disconnect(addr)
                if self.powered:
                    with contextlib.suppress(Exception):
                        await self._set_scan(False)
                    with contextlib.suppress(Exception):
                        await self.backend.power(False)
            self.powered = False
            self.scanning = False
        self._closing = False
