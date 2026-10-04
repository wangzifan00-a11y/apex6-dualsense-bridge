import json
from contextlib import contextmanager
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from apex6_hid import ReceiverError
from apex6_protocol import command_report, waveform_report
from apex6_session import Apex6Session, saved_motor_modes

MAPPING = bytes.fromhex("02020040060000000000000002004006000000000000000140014000000000000200000000000000000000000000000000000000000000000000000000000000")


@contextmanager
def preserved_test_folder():
    folder = Path(__file__).resolve().parents[1] / "测试记录" / "接收器自检" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    yield folder  # User policy: retain records; no recursive cleanup.


class FakeTransport:
    def __init__(self, path):
        self.path, self.closed, self.writes = path, False, []
    def write(self, report):
        self.writes.append(report)
    def close(self):
        self.closed = True


class FakeClaim:
    closed = False
    def close(self):
        self.closed = True


class FakeProtocol:
    def __init__(self, transport):
        self.transport = transport
        self.config = []
        self.fail_setup = False
        self.fail_restore = False
        self.uid = "abc"
        self.query_count = 0
    def identity(self):
        return {"uid": self.uid}
    def motor_mapping(self):
        return 0, MAPPING
    def slot(self):
        return 0
    def mode(self, target, mode, params=b""):
        self.config.append((target, mode, params))
        if self.fail_setup and target == 0x11 and mode == 2:
            raise ReceiverError("ACK lost after setup")
        if self.fail_restore and target == 0 and mode == 0:
            raise ReceiverError("ACK lost after restore")


class ReceiverProtocolTests(unittest.TestCase):
    def test_private_packet_has_report_placeholder_checksum_and_isolated_lanes(self):
        packet = waveform_report((20,) * 8, (-20,) * 8, (8,) * 8, selector=1, trigger_enabled=True)
        self.assertEqual(len(packet), 33)
        self.assertEqual(packet[:6], bytes.fromhex("005aa5571b9d"))
        self.assertEqual(packet[6:30], bytes.fromhex("88946c") * 8)
        self.assertEqual(packet[30:32], b"\0\0")
        self.assertEqual(packet[32], sum(packet[3:32]) % 256)
        muted = waveform_report((0,) * 8, (0,) * 8, (127,) * 8)
        self.assertEqual(muted[6:30], b"\x80" * 24)
    def test_unrestorable_mapping_is_refused_before_any_configuration(self):
        for offset in (0, 32, 33, 44, 55, 57):
            data = bytearray(MAPPING)
            data[offset] = 250
            with self.assertRaises(ReceiverError):
                saved_motor_modes(data)
    def test_profile_flash_commands_are_absent_from_session(self):
        self.assertEqual(command_report(0x53, bytes.fromhex("0112024000"))[:10], bytes.fromhex("005aa553070112024000"))

    def make(self, folder):
        return Apex6Session(Path(folder), transport_factory=FakeTransport, claim_factory=FakeClaim)

    def test_stop_restores_all_four_original_modes_and_releases_claim(self):
        with preserved_test_folder() as folder, patch("apex6_session.ReceiverProtocol", FakeProtocol), patch("apex6_session.time.sleep"):
            session = self.make(folder)
            session.start("fake")
            transport, protocol, claim = session.transport, session.protocol, session.claim
            self.assertTrue(json.loads(session.journal_path.read_text())["restore_pending"])
            session.send((1,) * 8)
            session.stop()
            self.assertEqual(protocol.config[-4:], saved_motor_modes(MAPPING))
            self.assertTrue(session.restored and transport.closed and claim.closed)
            self.assertFalse(json.loads(session.journal_path.read_text())["restore_pending"])
            self.assertFalse(any(report[3] == 0xA4 for report in transport.writes))

    def test_failed_setup_ack_still_restores_the_pad(self):
        class SetupFailure(FakeProtocol):
            def __init__(self, transport):
                super().__init__(transport)
                self.fail_setup = True
        with preserved_test_folder() as folder, patch("apex6_session.ReceiverProtocol", SetupFailure):
            session = self.make(folder)
            with self.assertRaises(ReceiverError):
                session.start("fake")
            self.assertTrue(session.restored)
            self.assertEqual(session.protocol.config[-4:], saved_motor_modes(MAPPING))
            self.assertIsNone(session.transport)

    def test_restore_record_contains_post_restore_readback(self):
        class ChangingMapping(FakeProtocol):
            def __init__(self, transport):
                super().__init__(transport)
                self.mapping = bytearray(MAPPING)
            def motor_mapping(self):
                return 0, bytes(self.mapping)
            def mode(self, target, mode, params=b""):
                super().mode(target, mode, params)
                offset = {0: 33, 1: 44, 0x10: 55, 0x11: 57}[target]
                width = 11 if target < 2 else 2
                self.mapping[offset:offset + width] = (bytes((mode,)) + params).ljust(width, b"\0")[:width]
        with preserved_test_folder() as folder, patch("apex6_session.ReceiverProtocol", ChangingMapping), patch("apex6_session.time.sleep"):
            session = self.make(folder)
            session.start("fake")
            active_mapping = bytes(session.protocol.mapping)
            self.assertNotEqual(active_mapping, MAPPING)
            session.stop()
            journal = json.loads(session.journal_path.read_text())
            self.assertTrue(session.restored)
            self.assertEqual(journal["restored_mapping_hex"], MAPPING.hex())

    def test_one_failed_restore_does_not_skip_the_other_three_motors(self):
        with preserved_test_folder() as folder, patch("apex6_session.ReceiverProtocol", FakeProtocol), patch("apex6_session.time.sleep"):
            session = self.make(folder)
            session.start("fake")
            session.protocol.fail_restore = True
            session.stop()
            self.assertEqual(session.protocol.config[-4:], saved_motor_modes(MAPPING))
            self.assertTrue(session.cleanup_errors)
            self.assertTrue(json.loads(session.journal_path.read_text())["restore_pending"])

    def test_replacement_pad_never_receives_old_configuration(self):
        with preserved_test_folder() as folder, patch("apex6_session.ReceiverProtocol", FakeProtocol), patch("apex6_session.time.sleep"):
            session = self.make(folder)
            session.start("fake")
            before = list(session.protocol.config)
            session.protocol.uid = "other"
            session.stop()
            self.assertEqual(session.protocol.config, before)
            self.assertFalse(session.restored)
            self.assertTrue(json.loads(session.journal_path.read_text())["restore_pending"])


if __name__ == "__main__":
    unittest.main()
