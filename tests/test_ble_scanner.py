import struct
import unittest

from linux_voice_assistant.ble_scanner import (
    BleAdvertisementBatcher,
    bluez_advertisement,
    parse_le_advertising_reports,
)


def advertising_packet(*reports: tuple[int, int, bytes, bytes, int]) -> bytes:
    payload = bytearray([0x02, len(reports)])
    for event_type, address_type, address, data, rssi in reports:
        payload.extend((event_type, address_type))
        payload.extend(address)
        payload.append(len(data))
        payload.extend(data)
        payload.extend(struct.pack("b", rssi))
    return bytes([0x04, 0x3E, len(payload)]) + bytes(payload)


class _FakeDevice:
    address = "11:22:33:44:55:66"
    details = {"AddressType": "random"}


class _FakeAdvertisement:
    manufacturer_data = {0x004C: b"\x02\x15"}
    service_data = {"0000180f-0000-1000-8000-00805f9b34fb": b"\x64"}
    service_uuids = ["180a"]
    local_name = "Tater Tag"
    tx_power = -8
    rssi = -61
    platform_data = ()


class BleScannerTests(unittest.TestCase):
    def test_decodes_legacy_advertising_reports(self) -> None:
        packet = advertising_packet(
            (0, 1, bytes.fromhex("665544332211"), b"\x05\x09Tater", -61),
            (4, 0, bytes.fromhex("ffeeddccbbaa"), b"\x02\x01\x06", -88),
        )

        reports = parse_le_advertising_reports(packet)

        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["address"], "11:22:33:44:55:66")
        self.assertEqual(reports[0]["address_type"], 1)
        self.assertEqual(reports[0]["rssi"], -61)
        self.assertEqual(reports[0]["data"], b"\x05\x09Tater")
        self.assertEqual(reports[1]["event_type"], 4)

    def test_rejects_truncated_packet_as_a_whole(self) -> None:
        packet = advertising_packet(
            (0, 0, bytes.fromhex("060504030201"), b"\x02\x01\x06", -50),
        )

        self.assertEqual(parse_le_advertising_reports(packet[:-1]), [])

    def test_bluez_fields_reconstruct_tater_advertisement_data(self) -> None:
        advert = bluez_advertisement(_FakeDevice(), _FakeAdvertisement())

        self.assertEqual(advert["address"], "11:22:33:44:55:66")
        self.assertEqual(advert["address_type"], 1)
        self.assertEqual(advert["rssi"], -61)
        self.assertIn(b"\xffL\x00\x02\x15", advert["data"])
        self.assertLessEqual(len(advert["data"]), 31)

    def test_batches_match_tater_contract_and_dedupe(self) -> None:
        batches = []
        batcher = BleAdvertisementBatcher(batches.append)
        advert = {
            "address": "11:22:33:44:55:66",
            "address_type": 1,
            "rssi": -60,
            "event_type": 0,
            "data": b"\x02\x01\x06",
        }

        self.assertTrue(batcher.ingest(advert, 1000))
        self.assertFalse(batcher.ingest(advert, 1100))
        self.assertTrue(batcher.ingest(dict(advert, rssi=-65), 1300))
        self.assertTrue(batcher.flush(1500, force=True))

        self.assertEqual(
            batches,
            [
                {
                    "version": 1,
                    "batch_id": 1,
                    "device_uptime_ms": 1500,
                    "adverts": [
                        {
                            "address": "11:22:33:44:55:66",
                            "address_type": 1,
                            "rssi": -65,
                            "event_type": 0,
                            "data": "020106",
                            "age_ms": 200,
                        }
                    ],
                }
            ],
        )

    def test_batch_and_dedupe_storage_are_bounded(self) -> None:
        batcher = BleAdvertisementBatcher(lambda _payload: None)
        for index in range(80):
            batcher.ingest(
                {
                    "address": f"02:00:00:00:{index // 256:02x}:{index % 256:02x}",
                    "address_type": 0,
                    "rssi": -70,
                    "event_type": 0,
                    "data": bytes([index % 256]),
                },
                1000 + index,
            )

        self.assertLessEqual(len(batcher._pending), 24)  # pylint: disable=protected-access
        self.assertLessEqual(len(batcher._dedupe), 48)  # pylint: disable=protected-access
        self.assertGreater(batcher.status()["adverts_dropped"], 0)


if __name__ == "__main__":
    unittest.main()
