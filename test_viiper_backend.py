"""Offline protocol and local-socket checks; no installed driver is required."""

import json
import socket
import struct
import threading
import time
import unittest
import zlib
from unittest.mock import patch

from viiper_backend import (ATOMIC_SIZE, DEVICE_TYPE, FEEDBACK_SIZE, FrameDecoder,
                            ProtocolError, ViiperBackend, ViiperError,
                            decode_haptics, encode_frame, encode_input)


def carrier_feedback(left=0, right=0, *, large=0, small=0):
    """Synthetic known-layout carrier, not a physical/game compatibility claim."""
    result = bytearray(FEEDBACK_SIZE)
    result[0], result[1] = small, large
    carrier = bytearray(398)
    carrier[0] = 0x36
    carrier[2:5] = b"\x91\x07\xfe"
    carrier[5:10] = bytes([16] * 5)
    carrier[11:13] = b"\x90\x3f"
    carrier[76:78] = b"\x92\x40"
    carrier[78:142] = struct.pack("<64b", *([left, right] * 32))
    carrier[142:144] = b"\x93\0"
    struct.pack_into("<I", carrier, 394, zlib.crc32(carrier[:394], 0xEADA2D49))
    result[76:] = carrier
    return bytes(result)


def rumble_feedback(large, small, flags=1):
    result = bytearray(FEEDBACK_SIZE)
    result[0], result[1] = small, large
    result[28] = 0x02
    result[29] = flags
    result[31], result[32] = small, large
    return bytes(result)


def atomic(feedback, speaker=bytes(1920)):
    return struct.pack("<H", FEEDBACK_SIZE) + feedback + speaker


class FakeViiper:
    """A test-only localhost server with explicit bus ownership recording."""

    def __init__(self, *, invalid_type=False, attach=True):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.stream_ready = threading.Event()
        self.input_ready = threading.Event()
        self.stream_condition = threading.Condition()
        self.stream = None
        self.commands = []
        self.inputs = bytearray()
        self.invalid_type, self.attach = invalid_type, attach
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = bytearray()
            while not data.endswith(b"\0"):
                part = conn.recv(1)
                if not part:
                    return
                data.extend(part)
            command = data[:-1].decode()
            self.commands.append(command)
            if command == "bus/7/2":
                with self.stream_condition:
                    self.stream = conn
                    self.stream_ready.set()
                    self.stream_condition.notify_all()
                while not self.stopped.is_set():
                    part = conn.recv(8192)
                    if not part:
                        break
                    with self.stream_condition:
                        if self.stream is conn:
                            self.inputs.extend(part)
                            self.input_ready.set()
                return
            if command == "ping":
                result = {"server": "VIIPER", "version": "0.1.9-rc4.6.6"}
            elif command == "bus/create":
                result = {"busId": 7}
            elif command.startswith("bus/7/add "):
                if self.invalid_type:
                    result = {"status": 400, "title": "Bad Request", "detail": "unknown device type"}
                else:
                    # A restarted client has a new stream generation. An old
                    # ready event/socket must never satisfy the new handshake.
                    with self.stream_condition:
                        self.stream = None
                        self.stream_ready.clear()
                        self.input_ready.clear()
                    result = {"busId": 7, "devId": "2", "vid": "0x054c", "pid": "0x0ce6", "type": DEVICE_TYPE}
                    if self.attach:
                        result["usbipPort"] = 9
            elif command == "bus/7/remove 2":
                result = {"busId": 7, "devId": "2"}
            elif command == "bus/remove 7":
                result = {"busId": 7}
            else:
                result = {"status": 404, "title": "Not Found", "detail": command}
            conn.sendall(json.dumps(result).encode() + b"\n")
        except OSError:
            pass
        finally:
            with self.stream_condition:
                if self.stream is conn:
                    self.stream = None
                    self.stream_ready.clear()
                    self.stream_condition.notify_all()
            conn.close()

    def _connected_stream(self):
        with self.stream_condition:
            if not self.stream_condition.wait_for(
                    lambda: self.stream is not None and self.stream_ready.is_set(), timeout=1):
                raise AssertionError("Fake device handshake did not establish a live stream")
            return self.stream

    def send(self, packet):
        conn = self._connected_stream()
        # Fragmentation crosses both the 16-byte header and audio payload.
        for first, last in ((0, 3), (3, 19), (19, len(packet))):
            conn.sendall(packet[first:last])

    def close_stream(self, *, require_connected=True):
        # start() can return before the test server parses the stream handshake.
        # A requested disconnect must wait for that handshake instead of being
        # a silent no-op when self.stream is still None.
        if require_connected:
            conn = self._connected_stream()
        else:
            with self.stream_condition:
                conn = self.stream
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                if require_connected:
                    raise AssertionError("Requested fake stream disconnect did not execute") from exc
        return conn

    def close(self):
        self.stopped.set()
        self.close_stream(require_connected=False)
        self.listener.close()
        self.thread.join(1)


class ProtocolTests(unittest.TestCase):
    def test_input_mapping_and_inactive_touch(self):
        payload = encode_input({"buttons": 0x1000 | 0x4000 | 1 | 0x0200,
                                "left_trigger": 255, "right_trigger": 127,
                                "lx": -32768, "ly": 32767,
                                "rx": 32767, "ry": -32768})
        self.assertEqual(len(payload), 33)
        self.assertEqual(struct.unpack_from("<4b", payload), (-128, -128, 127, 127))
        self.assertEqual(struct.unpack_from("<I", payload, 4)[0], 0x20 | 0x10 | 0x200 | 0x400 | 0x800)
        self.assertEqual(payload[8:11], b"\x01\xff\x7f")
        self.assertEqual((payload[15], payload[20]), (0x80, 0x80))
        self.assertEqual(struct.unpack_from("<h", payload, 31)[0], -8192)

    def test_fragmented_and_coalesced_frames(self):
        packet = encode_frame(0x81, rumble_feedback(255, 64), 0) + encode_frame(0x84, carrier_feedback(64, -32), 1)
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(packet[:9]), [])
        self.assertEqual(decoder.feed(packet[9:100]), [])
        result = decoder.feed(packet[100:])
        self.assertEqual([kind for kind, _ in result], [0x81, 0x84])

    def test_crc_version_unknown_length_and_sequence_fail_closed(self):
        packet = encode_frame(0x81, bytes(FEEDBACK_SIZE), 0)
        corrupt = bytearray(packet)
        corrupt[-1] ^= 1
        bad_version = bytearray(packet)
        bad_version[4] = 4
        for bad in (corrupt, bad_version, encode_frame(0x82, b"", 0),
                    encode_frame(0x81, b"\0", 0), encode_frame(0x81, bytes(FEEDBACK_SIZE), 9)):
            with self.subTest(header=bytes(bad[:16])):
                with self.assertRaises(ProtocolError):
                    FrameDecoder().feed(bad)

    def test_signed_rear_channel_rms_only(self):
        self.assertEqual(decode_haptics(carrier_feedback(-64, 32)), (0.5, 0.25))
        self.assertEqual(decode_haptics(carrier_feedback(-128, 0)), (1, 0))
        self.assertIsNone(decode_haptics(bytes(FEEDBACK_SIZE)))
        invalid = bytearray(carrier_feedback(64, 64))
        invalid[76 + 78] ^= 1
        with self.assertRaises(ProtocolError):
            decode_haptics(invalid)

    def test_speaker_audio_never_causes_vibration(self):
        backend = ViiperBackend()
        backend._apply_feedback(0x83, atomic(carrier_feedback(), b"\xff\x7f" * 960))
        self.assertEqual(backend.poll_feedback(), (0, 0))
        self.assertEqual(backend.status()["native_haptics_nonzero_frames"], 0)
        self.assertEqual(backend.status()["native_haptics_peak_rms"], (0, 0))
        self.assertEqual(ATOMIC_SIZE, 2396)

    def test_statistics_distinguish_silent_streams_from_authored_feedback(self):
        backend = ViiperBackend()
        backend._apply_feedback(0x84, carrier_feedback())
        backend._apply_feedback(0x81, rumble_feedback(0, 0))
        status = backend.status()
        self.assertEqual(status["native_haptics_frames"], 1)
        self.assertEqual(status["rumble_commands"], 1)
        self.assertEqual(status["native_haptics_nonzero_frames"], 0)
        self.assertEqual(status["rumble_nonzero_commands"], 0)
        backend._apply_feedback(0x84, carrier_feedback(64, -32))
        backend._apply_feedback(0x81, rumble_feedback(128, 64))
        backend._apply_feedback(0x84, carrier_feedback())
        backend._apply_feedback(0x81, rumble_feedback(0, 0))
        status = backend.status()
        self.assertEqual(status["native_haptics_nonzero_frames"], 1)
        self.assertEqual(status["rumble_nonzero_commands"], 1)
        self.assertEqual(status["native_haptics_peak_rms"], (0.5, 0.25))
        self.assertEqual(status["rumble_peak"], (128 / 255, 64 / 255))

    def test_statistics_ignore_discarded_audio_and_led_only_commands(self):
        backend = ViiperBackend()
        backend._apply_feedback(0x84, carrier_feedback())
        backend._apply_feedback(0x83, atomic(carrier_feedback(127, 127)))
        backend._apply_feedback(0x81, rumble_feedback(255, 255, flags=0))
        self.assertEqual(backend.status()["native_haptics_nonzero_frames"], 0)
        self.assertEqual(backend.status()["rumble_nonzero_commands"], 0)

    def test_rumble_preserved_by_silent_audio_and_led_commands(self):
        backend = ViiperBackend()
        backend._apply_feedback(0x81, rumble_feedback(200, 100))
        backend._apply_feedback(0x83, atomic(carrier_feedback()))
        backend._apply_feedback(0x81, rumble_feedback(0, 0, flags=0))
        self.assertEqual(backend.poll_feedback(), (200 / 255, 100 / 255))
        backend._apply_feedback(0x81, rumble_feedback(0, 0))
        self.assertEqual(backend.poll_feedback(), (0, 0))

    def test_realtime_pcm_not_erased_by_atomic_zero_fill(self):
        backend = ViiperBackend()
        backend._apply_feedback(0x84, carrier_feedback(64, 32))
        backend._apply_feedback(0x83, atomic(carrier_feedback()))
        self.assertEqual(backend.poll_feedback(), (0.5, 0.25))
        with patch("viiper_backend.time.monotonic", return_value=backend._haptics_at + 1):
            self.assertEqual(backend.poll_feedback(), (0, 0))

    def test_legacy_and_remote_servers_rejected(self):
        with self.assertRaises(ValueError):
            ViiperBackend(host="192.168.1.1")
        with self.assertRaises(ValueError):
            ViiperBackend(device_type="dualsense")

    def test_invalid_controller_identity_fails_before_connecting(self):
        invalid = ("", "0" * 31, "0" * 33, "g" * 32, " " + "0" * 31,
                   "Ｆ" * 32, 123, True)
        with patch("viiper_backend.socket.create_connection") as connect:
            for identity in invalid:
                with self.subTest(identity=identity):
                    with self.assertRaisesRegex(ValueError, "controller_identity"):
                        ViiperBackend(controller_identity=identity)
            connect.assert_not_called()
        self.assertIsNone(ViiperBackend().controller_identity)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeViiper()
        self.backend = ViiperBackend(port=self.server.port, timeout=0.5)

    def tearDown(self):
        self.backend.stop()
        self.server.close()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for localhost protocol event")

    def test_create_update_feedback_and_owned_cleanup(self):
        self.backend.start()
        self.assertTrue(self.server.input_ready.wait(1))
        self.backend.update({"buttons": 0x1000})
        self.server.send(encode_frame(0x81, rumble_feedback(255, 127), 0))
        self.wait_for(lambda: self.backend.status()["feedback_frames"] == 1)
        self.assertEqual(self.backend.poll_feedback(), (1, 127 / 255))
        self.assertEqual(self.backend.status()["rumble_nonzero_commands"], 1)
        self.assertIsNone(self.backend.poll_feedback())
        self.server.send(encode_frame(0x84, carrier_feedback(64, 64), 1))
        self.wait_for(lambda: self.backend.status()["native_haptics_frames"] == 1)
        self.assertEqual(self.backend.poll_feedback(), (1, 0.5))
        self.backend.stop()
        self.assertIn("bus/7/remove 2", self.server.commands)
        self.assertIn("bus/remove 7", self.server.commands)
        self.assertTrue(all("bus/8" not in cmd for cmd in self.server.commands))
        self.assertIsNone(self.backend.bus_id)

    def test_persisted_controller_identity_survives_restart(self):
        identity = "0123456789abcdef0123456789abcdef"
        self.backend = ViiperBackend(port=self.server.port, timeout=0.5,
                                     controller_identity=identity)
        self.backend.start()
        self.backend.stop()
        self.backend.start()
        requests = [json.loads(command.split(" ", 1)[1]) for command in self.server.commands
                    if command.startswith("bus/7/add ")]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["deviceSpecific"], requests[1]["deviceSpecific"])
        self.assertEqual(requests[0]["deviceSpecific"], {
            "serial_number": "APEX010123456789", "mac_address": "02:01:23:45:67:89"})
        self.assertEqual(self.backend.controller_identity, identity.upper())

    def test_disconnect_surfaces_live_error_and_restart_clears_feedback(self):
        self.backend.start()
        disconnected = self.server.close_stream()
        self.assertIsNotNone(disconnected)
        self.wait_for(lambda: self.backend.error is not None)
        with self.assertRaises(ViiperError):
            self.backend.poll_feedback()
        self.assertEqual(self.backend._rumble, (0, 0))
        self.backend.stop()
        self.backend.start()
        # Receiving a fresh sequence-0 feedback packet proves a new handshake,
        # new decoder sequence and live receiver, beyond an error reset alone.
        self.server.send(encode_frame(0x81, rumble_feedback(128, 64), 0))
        self.wait_for(lambda: self.backend.status()["feedback_frames"] == 1)
        self.assertIsNone(self.backend.error)
        self.assertEqual(self.backend.poll_feedback(), (128 / 255, 64 / 255))
        self.assertEqual(self.backend.status()["rumble_nonzero_commands"], 1)
        self.assertEqual(self.backend.status()["native_haptics_nonzero_frames"], 0)

    def test_corruption_stops_output(self):
        self.backend.start()
        corrupt = bytearray(encode_frame(0x84, carrier_feedback(127, 127), 0))
        corrupt[-1] ^= 1
        self.server.send(corrupt)
        self.wait_for(lambda: self.backend.error is not None)
        with self.assertRaises(ViiperError):
            self.backend.update({})
        self.assertEqual(self.backend._haptics, (0, 0))

    def test_missing_native_attach_cleanup(self):
        self.server.attach = False
        with patch("viiper_backend.sys.platform", "win32"):
            with self.assertRaisesRegex(ViiperError, "没有确认 Windows"):
                self.backend.start()
        self.assertIn("bus/remove 7", self.server.commands)

    def test_unknown_v5_device_cleanup(self):
        self.server.invalid_type = True
        with self.assertRaisesRegex(ViiperError, "unknown device type"):
            self.backend.start()
        self.assertIn("bus/remove 7", self.server.commands)


if __name__ == "__main__":
    unittest.main()
