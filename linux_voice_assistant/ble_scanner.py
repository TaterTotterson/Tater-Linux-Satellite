"""Bounded BLE observation for Tater-native Linux satellites.

Linux appliances that grant the voice process ``CAP_NET_RAW`` and
``CAP_NET_ADMIN`` use the kernel HCI socket directly. User-level applications
fall back to Bleak's BlueZ D-Bus backend when it is installed. Both paths feed
the same bounded batching and deduplication layer and never connect or pair.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import errno
import fcntl
import logging
import socket
import struct
import sys
import threading
import time
from typing import Any, Callable, Optional
from uuid import UUID

_LOGGER = logging.getLogger(__name__)

_AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", 31)
_BTPROTO_HCI = getattr(socket, "BTPROTO_HCI", 1)
_SOL_HCI = 0
_HCI_FILTER = 2
_HCI_COMMAND_PKT = 0x01
_HCI_EVENT_PKT = 0x04
_EVT_LE_META_EVENT = 0x3E
_EVT_LE_ADVERTISING_REPORT = 0x02
_OGF_LE_CTL = 0x08
_OCF_LE_SET_SCAN_PARAMETERS = 0x000B
_OCF_LE_SET_SCAN_ENABLE = 0x000C
_HCIDEVUP = 0x400448C9

_SCAN_INTERVAL_UNITS = 512  # 320 ms, matching Tater's constrained ESP targets.
_SCAN_WINDOW_UNITS = 48  # 30 ms, about a 9.4% receive duty cycle.
_BATCH_MAX = 24
_DEDUPE_MAX = 48
_DATA_MAX = 31
_RSSI_DELTA_DB = 3
_EMIT_MIN_MS = 250
_EMIT_MAX_MS = 1000
_BATCH_FLUSH_MS = 500
_RETRY_SECONDS = 2.0


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def _opcode(ogf: int, ocf: int) -> int:
    return (int(ogf) << 10) | int(ocf)


def _hci_command(ogf: int, ocf: int, parameters: bytes) -> bytes:
    return struct.pack("<BHB", _HCI_COMMAND_PKT, _opcode(ogf, ocf), len(parameters)) + parameters


def parse_le_advertising_reports(packet: bytes) -> list[dict[str, Any]]:
    """Decode legacy LE advertising reports from a raw HCI event packet."""
    raw = bytes(packet or b"")
    if len(raw) < 5 or raw[0] != _HCI_EVENT_PKT or raw[1] != _EVT_LE_META_EVENT:
        return []
    packet_end = 3 + raw[2]
    if packet_end > len(raw) or raw[3] != _EVT_LE_ADVERTISING_REPORT:
        return []

    report_count = raw[4]
    offset = 5
    reports: list[dict[str, Any]] = []
    for _index in range(report_count):
        if offset + 10 > packet_end:
            return []
        event_type = raw[offset]
        address_type = raw[offset + 1]
        address_bytes = raw[offset + 2 : offset + 8]
        data_length = raw[offset + 8]
        data_start = offset + 9
        data_end = data_start + data_length
        if data_end >= packet_end:
            return []
        data = raw[data_start:data_end]
        rssi = struct.unpack("b", raw[data_end : data_end + 1])[0]
        reports.append(
            {
                "address": ":".join(f"{value:02x}" for value in reversed(address_bytes)),
                "address_type": int(address_type),
                "rssi": int(rssi),
                "event_type": int(event_type),
                "data": bytes(data[:_DATA_MAX]),
            }
        )
        offset = data_end + 1
    return reports


def _uuid_advertisement(uuid_text: str) -> tuple[int, bytes] | None:
    """Encode a BlueZ UUID as its shortest Bluetooth advertisement form."""
    text = str(uuid_text or "").strip().lower()
    suffix = "-0000-1000-8000-00805f9b34fb"
    try:
        if len(text) == 4:
            return 0x03, int(text, 16).to_bytes(2, "little")
        if len(text) == 8:
            return 0x05, int(text, 16).to_bytes(4, "little")
        if text.startswith("0000") and text.endswith(suffix):
            return 0x03, int(text[4:8], 16).to_bytes(2, "little")
        if text.endswith(suffix):
            return 0x05, int(text[:8], 16).to_bytes(4, "little")
        return 0x07, UUID(text).bytes[::-1]
    except (ValueError, OverflowError):
        return None


def _append_ad_structure(target: bytearray, field_type: int, payload: bytes) -> bool:
    value = bytes(payload or b"")
    available = _DATA_MAX - len(target)
    if available < 3 or not value:
        return False
    value = value[: available - 2]
    if not value:
        return False
    target.extend((len(value) + 1, int(field_type) & 0xFF))
    target.extend(value)
    return True


def bluez_advertisement(device: Any, advertisement_data: Any) -> dict[str, Any]:
    """Convert Bleak's stable fields into Tater's legacy advertisement row."""
    address = str(getattr(device, "address", "") or "").strip().lower()
    details: dict[str, Any] = {}
    raw_details = getattr(device, "details", None)
    if isinstance(raw_details, dict):
        details.update(raw_details)
    for item in tuple(getattr(advertisement_data, "platform_data", ()) or ()):
        if isinstance(item, dict):
            details.update(item)
    address_type = 1 if str(details.get("AddressType") or "").strip().lower() == "random" else 0

    payload = bytearray()
    manufacturer_data = getattr(advertisement_data, "manufacturer_data", {}) or {}
    for company_id, value in sorted(manufacturer_data.items(), key=lambda item: int(item[0])):
        try:
            company = max(0, min(0xFFFF, int(company_id))).to_bytes(2, "little")
        except (TypeError, ValueError, OverflowError):
            continue
        if not _append_ad_structure(payload, 0xFF, company + bytes(value or b"")):
            break

    service_data = getattr(advertisement_data, "service_data", {}) or {}
    for uuid_text, value in sorted(service_data.items(), key=lambda item: str(item[0])):
        encoded = _uuid_advertisement(str(uuid_text))
        if encoded is None:
            continue
        uuid_type, uuid_bytes = encoded
        service_type = {0x03: 0x16, 0x05: 0x20, 0x07: 0x21}[uuid_type]
        if not _append_ad_structure(payload, service_type, uuid_bytes + bytes(value or b"")):
            break

    for uuid_text in list(getattr(advertisement_data, "service_uuids", []) or []):
        encoded = _uuid_advertisement(str(uuid_text))
        if encoded is not None and not _append_ad_structure(payload, encoded[0], encoded[1]):
            break

    local_name = str(getattr(advertisement_data, "local_name", "") or "").strip()
    if local_name:
        _append_ad_structure(payload, 0x09, local_name.encode("utf-8", errors="ignore"))
    tx_power = getattr(advertisement_data, "tx_power", None)
    if tx_power is not None:
        try:
            _append_ad_structure(payload, 0x0A, struct.pack("b", max(-127, min(127, int(tx_power)))))
        except (TypeError, ValueError, OverflowError):
            pass

    try:
        rssi = int(getattr(advertisement_data, "rssi", -127))
    except (TypeError, ValueError, OverflowError):
        rssi = -127
    return {
        "address": address,
        "address_type": address_type,
        "rssi": rssi,
        "event_type": 0,
        "data": bytes(payload[:_DATA_MAX]),
    }


class BleAdvertisementBatcher:
    """Thread-safe fixed-memory formatter for ``ble.advertisements`` v1."""

    def __init__(self, on_batch: Callable[[dict[str, Any]], None]) -> None:
        self.on_batch = on_batch
        self._lock = threading.RLock()
        self._pending: "OrderedDict[tuple[str, int], dict[str, Any]]" = OrderedDict()
        self._dedupe: "OrderedDict[tuple[str, int, int, bytes], tuple[int, int]]" = OrderedDict()
        self._last_flush_ms = _monotonic_ms()
        self._batch_id = 0
        self.stats: dict[str, int] = {
            "adverts_seen": 0,
            "adverts_filtered": 0,
            "adverts_dropped": 0,
            "batches_sent": 0,
        }

    def clear_pending(self) -> None:
        with self._lock:
            self._pending.clear()

    def status(self) -> dict[str, int]:
        with self._lock:
            return dict(self.stats)

    def ingest(self, advert: dict[str, Any], now_ms: Optional[int] = None) -> bool:
        observed_ms = _monotonic_ms() if now_ms is None else int(now_ms)
        with self._lock:
            self.stats["adverts_seen"] += 1
            address = str(advert.get("address") or "").lower()
            address_type = int(advert.get("address_type") or 0)
            event_type = int(advert.get("event_type") or 0)
            rssi = int(advert.get("rssi") or -127)
            data = bytes(advert.get("data") or b"")[:_DATA_MAX]
            if not address:
                self.stats["adverts_dropped"] += 1
                return False
            dedupe_key = (address, address_type, event_type, data)
            previous = self._dedupe.get(dedupe_key)
            emit = previous is None
            if previous is not None:
                previous_ms, previous_rssi = previous
                elapsed_ms = max(0, observed_ms - previous_ms)
                emit = elapsed_ms >= _EMIT_MAX_MS or (abs(rssi - previous_rssi) >= _RSSI_DELTA_DB and elapsed_ms >= _EMIT_MIN_MS)
            if previous is None or emit:
                self._dedupe[dedupe_key] = (observed_ms, rssi)
            else:
                self._dedupe[dedupe_key] = previous
            self._dedupe.move_to_end(dedupe_key)
            while len(self._dedupe) > _DEDUPE_MAX:
                self._dedupe.popitem(last=False)
            if not emit:
                self.stats["adverts_filtered"] += 1
                return False

            pending_key = (address, event_type)
            if pending_key not in self._pending and len(self._pending) >= _BATCH_MAX:
                self.stats["adverts_dropped"] += 1
                return False
            self._pending[pending_key] = {
                "address": address,
                "address_type": max(0, min(3, address_type)),
                "rssi": max(-127, min(20, rssi)),
                "event_type": max(0, min(255, event_type)),
                "data": data.hex(),
                "observed_ms": observed_ms,
            }
            self._pending.move_to_end(pending_key)
            return True

    def flush(self, now_ms: Optional[int] = None, *, force: bool = False) -> bool:
        observed_ms = _monotonic_ms() if now_ms is None else int(now_ms)
        with self._lock:
            if not self._pending:
                return False
            if not force and len(self._pending) < _BATCH_MAX and observed_ms - self._last_flush_ms < _BATCH_FLUSH_MS:
                return False
            rows = []
            for advert in self._pending.values():
                row = dict(advert)
                first_seen_ms = int(row.pop("observed_ms"))
                row["age_ms"] = max(0, min(60_000, observed_ms - first_seen_ms))
                rows.append(row)
            self._pending.clear()
            self._last_flush_ms = observed_ms
            self._batch_id = (self._batch_id + 1) & 0xFFFFFFFF
            if self._batch_id == 0:
                self._batch_id = 1
            payload = {
                "version": 1,
                "batch_id": self._batch_id,
                "device_uptime_ms": observed_ms,
                "adverts": rows,
            }
        try:
            self.on_batch(payload)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Could not submit BLE advertisements to Tater")
            return False
        with self._lock:
            self.stats["batches_sent"] += 1
        return True


class _RawHciObserver:
    def __init__(
        self,
        batcher: BleAdvertisementBatcher,
        *,
        device_id: int,
        should_pause: Optional[Callable[[], bool]],
        status: dict[str, Any],
    ) -> None:
        self.batcher = batcher
        self.device_id = device_id
        self.should_pause = should_pause
        self.status = status
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._socket_lock = threading.Lock()
        self._socket: Optional[socket.socket] = None

    @staticmethod
    def _set_event_filter(hci_socket: socket.socket) -> None:
        hci_socket.setsockopt(
            _SOL_HCI,
            _HCI_FILTER,
            struct.pack("=IIIH", 1 << _HCI_EVENT_PKT, 0, 1 << (_EVT_LE_META_EVENT - 32), 0),
        )

    @staticmethod
    def _set_scan_enabled(hci_socket: socket.socket, enabled: bool) -> None:
        hci_socket.send(
            _hci_command(
                _OGF_LE_CTL,
                _OCF_LE_SET_SCAN_ENABLE,
                struct.pack("<BB", 1 if enabled else 0, 0),
            )
        )

    @staticmethod
    def _set_scan_parameters(hci_socket: socket.socket) -> None:
        hci_socket.send(
            _hci_command(
                _OGF_LE_CTL,
                _OCF_LE_SET_SCAN_PARAMETERS,
                struct.pack("<BHHBB", 0, _SCAN_INTERVAL_UNITS, _SCAN_WINDOW_UNITS, 0, 0),
            )
        )

    def _open_socket(self) -> socket.socket:
        control_socket = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)
        try:
            try:
                fcntl.ioctl(control_socket.fileno(), _HCIDEVUP, struct.pack("I", self.device_id))
            except OSError as exc:
                if exc.errno not in {errno.EALREADY, errno.EBUSY}:
                    raise
        finally:
            control_socket.close()

        hci_socket = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)
        hci_socket.bind((self.device_id,))
        hci_socket.settimeout(0.25)
        self._set_event_filter(hci_socket)
        try:
            self._set_scan_enabled(hci_socket, False)
        except OSError:
            pass
        self._set_scan_parameters(hci_socket)
        self._set_scan_enabled(hci_socket, True)
        return hci_socket

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tater-ble-hci", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._socket_lock:
            active_socket = self._socket
        if active_socket is not None:
            try:
                active_socket.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _pause_requested(self) -> bool:
        if self.should_pause is None:
            return False
        try:
            return bool(self.should_pause())
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("BLE pause predicate failed", exc_info=True)
            return True

    def _run(self) -> None:
        while not self._stop.is_set():
            hci_socket: Optional[socket.socket] = None
            try:
                hci_socket = self._open_socket()
                with self._socket_lock:
                    self._socket = hci_socket
                self.status.update({"running": True, "scanning": True, "paused": False, "last_error": ""})
                _LOGGER.info("Tater BLE observer started with raw HCI on hci%d", self.device_id)
                self._receive_loop(hci_socket)
            except Exception as exc:  # pylint: disable=broad-except
                if not self._stop.is_set():
                    self.status["last_error"] = str(exc)
                    self.status["controller_restarts"] = int(self.status["controller_restarts"]) + 1
                    _LOGGER.warning("Raw HCI BLE observer unavailable (%s); retrying", exc)
                    self._stop.wait(_RETRY_SECONDS)
            finally:
                self.status["scanning"] = False
                with self._socket_lock:
                    if self._socket is hci_socket:
                        self._socket = None
                if hci_socket is not None:
                    try:
                        self._set_scan_enabled(hci_socket, False)
                    except OSError:
                        pass
                    try:
                        hci_socket.close()
                    except OSError:
                        pass
        self.status.update({"running": False, "scanning": False, "paused": False})

    def _receive_loop(self, hci_socket: socket.socket) -> None:
        paused = False
        while not self._stop.is_set():
            pause_requested = self._pause_requested()
            if pause_requested != paused:
                paused = pause_requested
                self.status.update({"paused": paused, "scanning": not paused})
                if paused:
                    self._set_scan_enabled(hci_socket, False)
                    self.batcher.clear_pending()
                else:
                    self._set_scan_parameters(hci_socket)
                    self._set_scan_enabled(hci_socket, True)
            if paused:
                self._stop.wait(0.25)
                continue
            try:
                packet = hci_socket.recv(260)
            except socket.timeout:
                self.batcher.flush()
                continue
            now_ms = _monotonic_ms()
            for advert in parse_le_advertising_reports(packet):
                self.batcher.ingest(advert, now_ms)
            self.batcher.flush(now_ms)


class LinuxBleScanner:
    """Automatic raw-HCI/BlueZ observer with fixed memory and bandwidth bounds."""

    def __init__(
        self,
        on_batch: Callable[[dict[str, Any]], None],
        *,
        device_id: int = 0,
        should_pause: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.device_id = max(0, int(device_id))
        self.should_pause = should_pause
        self.batcher = BleAdvertisementBatcher(on_batch)
        self._raw: Optional[_RawHciObserver] = None
        self._bluez_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._status: dict[str, Any] = {
            "available": sys.platform.startswith("linux"),
            "backend": "",
            "running": False,
            "scanning": False,
            "paused": False,
            "controller_restarts": 0,
            "last_error": "",
        }

    def status(self) -> dict[str, Any]:
        return {**self._status, **self.batcher.status()}

    def _pause_requested(self) -> bool:
        if self.should_pause is None:
            return False
        try:
            return bool(self.should_pause())
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("BLE pause predicate failed", exc_info=True)
            return True

    def _raw_hci_available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        probe: Optional[socket.socket] = None
        try:
            probe = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)
            probe.bind((self.device_id,))
            return True
        except OSError:
            return False
        finally:
            if probe is not None:
                probe.close()

    async def start(self) -> None:
        if self._raw is not None or (self._bluez_task is not None and not self._bluez_task.done()):
            return
        self._stop = asyncio.Event()
        if self._raw_hci_available():
            self._status.update({"available": True, "backend": "raw_hci", "last_error": ""})
            self._raw = _RawHciObserver(
                self.batcher,
                device_id=self.device_id,
                should_pause=self.should_pause,
                status=self._status,
            )
            self._raw.start()
            return

        self._status["backend"] = "bluez"
        self._bluez_task = asyncio.create_task(self._run_bluez())
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stop.set()
        raw = self._raw
        self._raw = None
        if raw is not None:
            await asyncio.to_thread(raw.stop)
        task = self._bluez_task
        self._bluez_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.batcher.clear_pending()
        self._status.update({"running": False, "scanning": False, "paused": False})

    async def _run_bluez(self) -> None:
        try:
            from bleak import BleakScanner
        except ImportError:
            self._status.update(
                {
                    "available": False,
                    "running": False,
                    "last_error": "Bleak is not installed and raw HCI access is unavailable",
                }
            )
            _LOGGER.info("Tater BLE observer disabled: %s", self._status["last_error"])
            return

        scanner: Any = None
        self._status.update({"available": True, "running": True})
        try:
            while not self._stop.is_set():
                if self._pause_requested():
                    self._status.update({"paused": True, "scanning": False})
                    self.batcher.clear_pending()
                    await asyncio.sleep(0.25)
                    continue
                self._status["paused"] = False
                try:
                    scanner = BleakScanner(
                        lambda device, advert: self.batcher.ingest(bluez_advertisement(device, advert)),
                        scanning_mode="active",
                        bluez={
                            "adapter": f"hci{self.device_id}",
                            "filters": {"Transport": "le", "DuplicateData": True},
                        },
                    )
                    await scanner.start()
                    self._status.update({"scanning": True, "last_error": ""})
                    _LOGGER.info("Tater BLE observer started through BlueZ on hci%d", self.device_id)
                    while not self._stop.is_set() and not self._pause_requested():
                        await asyncio.sleep(0.25)
                        self.batcher.flush()
                except Exception as exc:  # pylint: disable=broad-except
                    self._status["last_error"] = str(exc)
                    self._status["controller_restarts"] = int(self._status["controller_restarts"]) + 1
                    _LOGGER.warning("BlueZ BLE observer unavailable (%s); retrying", exc)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=_RETRY_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                finally:
                    self._status["scanning"] = False
                    if scanner is not None:
                        try:
                            await scanner.stop()
                        except Exception:  # pylint: disable=broad-except
                            _LOGGER.debug("Could not stop BlueZ BLE scan cleanly", exc_info=True)
                        scanner = None
        finally:
            self._status.update({"running": False, "scanning": False, "paused": False})
