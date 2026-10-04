"""Regression: detection and motor sessions share HID duplicate-report state."""
from collections import deque
import threading
import time
import unittest

from dsbridge.controllers.flydigi.apex6.hid import ReceiverError
from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol


def reply(command, data, *, ram=False):
    frame = bytearray(32)
    frame[:3] = bytes((0x5A, 0xA5, command))
    start = 3 if ram else 5
    if not ram:
        frame[3:5] = b"\1\0"
    frame[start:start + len(data)] = data
    frame[31] = sum(frame[2:31]) & 255
    return b"\0" + frame


class Receiver:
    """Duplicate suppression belongs to the device, not a Python/HID handle."""
    def __init__(self, *, silent=False, corrupt=False):
        self.last_reply = None
        self.writes = []
        self.silent, self.corrupt = silent, corrupt
        self.info = bytes((0x98, 1)) + bytes(22) + b"\x9f"
        self.uid = bytes(range(16))

    def open(self):
        device = self
        class Handle:
            def __init__(self):
                self.queue = deque()
            def write(self, report):
                command = report[3]
                device.writes.append(report)
                if device.silent or command == 0x53:
                    return
                data = {1: device.info, 4: device.uid, 0xA1: bytes(11),
                        0xA3: bytes((1, 0, 6, 0, 64, 0, 1, 2, 3, 4))}[command]
                packet = reply(command, data, ram=command == 0xA3)
                if device.corrupt:
                    packet = packet[:-1] + bytes((packet[-1] ^ 1,))
                if packet != device.last_reply:
                    self.queue.append(packet)
                    device.last_reply = packet
            def read(self, timeout_ms):
                if self.queue:
                    return self.queue.popleft()
                time.sleep(timeout_ms / 1000)
                return None
        return Handle()


class QueryRecoveryTests(unittest.TestCase):
    def protocol(self, device):
        return ReceiverProtocol(device.open(), reply_timeout=.01)

    def test_detection_then_new_motor_session_identity_recovers(self):
        device = Receiver()
        self.protocol(device).ask(1, 25)  # Final query made by auto-detection.
        session = self.protocol(device)  # Fresh handle has no previous-query cache.
        identity = session.identity()
        self.assertEqual(identity["uid"], device.uid.hex())
        self.assertEqual(identity["device_type"], 0x98)
        self.assertEqual(session.reply_recoveries, 1)
        self.assertEqual([r[3] for r in device.writes], [1, 1, 4, 1, 4])

    def test_repeated_queries_recover_without_configuration_commands(self):
        for command, length, payload, ram in ((1, 25, b"", False), (4, 16, b"", False),
                                              (0xA1, 11, b"", False), (0xA3, 10, b"\1\6", True)):
            with self.subTest(command=command):
                device = Receiver()
                protocol = self.protocol(device)
                first = protocol.ask(command, length, payload, ram=ram)
                self.assertEqual(protocol.ask(command, length, payload, ram=ram), first)
                self.assertEqual(protocol.reply_recoveries, 1)
                self.assertEqual(device.writes[0], device.writes[-1])
                self.assertTrue(all(r[3] in (1, 4, 0xA1, 0xA3) for r in device.writes))
                self.assertEqual(protocol.configuration_count, 0)

    def test_unresponsive_device_has_one_bounded_retry(self):
        device = Receiver(silent=True)
        protocol = self.protocol(device)
        with self.assertRaisesRegex(ReceiverError, "0x01"):
            protocol.ask(1, 25)
        self.assertEqual([r[3] for r in device.writes], [1, 4, 1])
        self.assertEqual(protocol.reply_recoveries, 0)

    def test_lost_mode_ack_is_never_replayed(self):
        device = Receiver()
        protocol = self.protocol(device)
        with self.assertRaisesRegex(ReceiverError, "0x53"):
            protocol.mode(0, 3, b"\0\x40")
        self.assertEqual([r[3] for r in device.writes], [1, 0x53])
        self.assertEqual(protocol.configuration_count, 0)

    def test_corrupt_reply_is_not_hidden_by_retry(self):
        device = Receiver(corrupt=True)
        with self.assertRaisesRegex(ReceiverError, "校验失败"):
            self.protocol(device).ask(1, 25)
        self.assertEqual(len(device.writes), 1)

    def test_transport_failure_is_not_retried(self):
        class Disconnected:
            writes = 0
            def write(self, report):
                self.writes += 1
                raise ReceiverError("接口已断开")
        transport = Disconnected()
        with self.assertRaisesRegex(ReceiverError, "已断开"):
            ReceiverProtocol(transport).ask(1, 25)
        self.assertEqual(transport.writes, 1)

    def test_unknown_ram_operation_is_not_replayed(self):
        device = Receiver(silent=True)
        with self.assertRaisesRegex(ReceiverError, "0xA3"):
            self.protocol(device).ask(0xA3, 10, b"\2\6", ram=True)
        self.assertEqual(len(device.writes), 1)

    def test_stop_cancels_recovery_before_any_extra_query(self):
        device = Receiver(silent=True)
        cancel = threading.Event()
        transport = device.open()
        def stopped_read(timeout_ms):
            cancel.set()
            return None
        transport.read = stopped_read
        with self.assertRaisesRegex(ReceiverError, "查询已停止"):
            ReceiverProtocol(transport, reply_timeout=.01, cancel=cancel).identity()
        self.assertEqual([r[3] for r in device.writes], [1])


if __name__ == "__main__":
    unittest.main()
