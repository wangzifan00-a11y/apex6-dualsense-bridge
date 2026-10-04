"""APEX 6 receiver command dialect, separate from the Bluetooth XInput path.

Wire facts: ApexSenseBridge dev f17bca8 and openflydigi's findings-apex6.md.
Only volatile motor routing is writable; profile/flash writes are not exposed.
"""
from __future__ import annotations

import struct
import time
import zlib

from dsbridge.controllers.flydigi.apex6.hid import ReceiverError


class _ReplyTimeout(ReceiverError):
    """A missing reply, distinct from disconnection or malformed data."""


def command_report(command, payload=b""):
    if isinstance(command, bool) or not isinstance(command, int) or not 0 <= command <= 255:
        raise ValueError("Invalid receiver command")
    payload = bytes(payload)
    if len(payload) > 27:
        raise ValueError("Receiver payload exceeds 27 bytes")
    frame = bytearray(32)
    frame[:4] = bytes((0x5A, 0xA5, command, len(payload) + 2))
    frame[4:4 + len(payload)] = payload
    frame[31] = sum(frame[2:31]) & 255
    return b"\0" + frame


def parse_reply(report, command, length, *, ram=False):
    if len(report) != 33 or report[0] != 0:
        raise ReceiverError("接收器应答不是已核实的 33 字节报告")
    frame = report[1:]
    if frame[:3] != bytes((0x5A, 0xA5, command)):
        return None
    start = 3 if ram else 5
    error = not ram and frame[3] == 0
    count = 1 if error else length
    end = start + count
    if not 0 <= count <= 27 or end > 31:
        raise ReceiverError("接收器应答长度无效")
    valid = any(
        check >= end and not any(frame[end:check]) and not any(frame[check + 1:])
        and (sum(frame[2:check]) & 255) == frame[check]
        for check in {end, 31}
    )
    if not valid:
        raise ReceiverError("接收器应答校验失败")
    if error:
        raise ReceiverError(f"接收器拒绝查询 0x{command:02X}：{frame[start]}")
    if not ram and frame[3:5] != b"\1\0":
        raise ReceiverError("接收器应答分包信息无效")
    return frame[start:end]


class ReceiverProtocol:
    def __init__(self, transport, *, reply_timeout=0.8, cancel=None):
        self.transport = transport
        self.reply_timeout = reply_timeout
        self.cancel = cancel
        self.query_count = 0
        self.configuration_count = 0
        self.reply_recoveries = 0
        self.last_mode_replies = []

    def ask(self, command, length, payload=b"", *, ram=False):
        try:
            return self._ask_once(command, length, payload, ram=ram)
        except _ReplyTimeout:
            # Identical input reports can be suppressed across HID handles.
            # Auto-detection ends with 0x01, so a newly opened motor session's
            # first 0x01 may receive nothing even though the pad is online.
            # Resynchronize only read-only requests; never replay a write whose
            # ACK was lost (notably 0x53, which may already have changed routing).
            readonly = (command in (0x01, 0x04, 0xA1) and not payload and not ram)
            readonly |= (command == 0xA3 and ram and (payload == b"\1\6" or
                         (len(payload) == 4 and payload[:2] == b"\0\6" and payload[2] < 4 and payload[3] == 16)))
            if not readonly:
                raise
        separator, separator_length = (0x04, 16) if command == 0x01 else (0x01, 25)
        try:
            self._ask_once(separator, separator_length)
        except _ReplyTimeout:
            # The separator itself can match the last report of an older
            # handle. The original request is now distinct; try it once only.
            pass
        result = self._ask_once(command, length, payload, ram=ram)
        self.reply_recoveries += 1
        return result

    def _ask_once(self, command, length, payload=b"", *, ram=False):
        self._check_cancel()
        self.transport.write(command_report(command, payload))
        self.query_count += 1
        deadline = time.monotonic() + self.reply_timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            reply = self.transport.read(max(1, round((deadline - time.monotonic()) * 1000)))
            if reply is not None:
                if command == 0x53 and reply[1:3] == b"\x5a\xa5" and reply[3] != 0xEF:
                    self.last_mode_replies.append(reply.hex())
                    self.last_mode_replies[:] = self.last_mode_replies[-16:]
                found = parse_reply(reply, command, length, ram=ram)
                if found is not None:
                    return found
        raise _ReplyTimeout(f"APEX 6 控制接口未应答 0x{command:02X}；请确认手柄已唤醒，并关闭其他占用手柄的软件后重试")

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise ReceiverError("接收器查询已停止")

    def identity(self):
        data = self.ask(1, 25)
        if data[0] not in (0x96, 0x98) or data[1] not in (1, 2) or data[24] & 0x90 != 0x90:
            raise ReceiverError("接收器身份待核实："
                                f"model={data[0]:02x}, connection={data[1]}, features={data[24]:02x}; "
                                f"info={data.hex()}")
        uid = self.ask(4, 16).hex()
        return {"device_type": data[0], "connection_mode": data[1],
                "features": data[24], "grip_haptics": bool(data[24] & 0x80),
                "trigger_haptics": bool(data[24] & 0x10), "uid": uid,
                "firmware": list(struct.unpack(">7H", data[10:24]))}

    def slot(self):
        slot = self.ask(0xA1, 11)[0]
        if slot > 3:
            raise ReceiverError("手柄配置槽无效")
        return slot

    def motor_mapping(self):
        slot = self.slot()
        query = self.ask(0xA3, 10, b"\1\6", ram=True)
        if query[:3] != b"\1\0\6":
            raise ReceiverError("马达映射查询失败")
        ram_slot, total, crc = struct.unpack("<BHI", query[3:])
        if ram_slot != slot or total != 64:
            raise ReceiverError("马达映射长度或配置槽不匹配")
        chunks = []
        for index in range(4):
            result = self.ask(0xA3, 24, bytes((0, 6, index, 16)), ram=True)
            if result[:8] != bytes((0, 0, 6, slot, 64, 0, index, 16)):
                raise ReceiverError("马达映射分段应答不匹配")
            chunks.append(result[8:])
        mapping = b"".join(chunks)
        after = self.ask(0xA3, 10, b"\1\6", ram=True)
        if self.slot() != slot or query != after or zlib.crc32(mapping) != crc:
            raise ReceiverError("读取时马达映射发生变化或 CRC32 无效")
        if mapping[0] != 2:
            raise ReceiverError("不支持此马达映射版本，未修改配置")
        return slot, mapping

    def mode(self, target, mode, parameters=b""):
        if target not in (0, 1, 2, 0x10, 0x11, 0x12):
            raise ValueError("Only the selected four motor targets are supported")
        # Mode ACKs are byte-identical. The measured receiver suppresses repeated
        # identical vendor input reports: interleave a distinct read-only reply.
        self.ask(1, 25)
        status = self.ask(0x53, 1, bytes((1, target, mode)) + bytes(parameters))[0]
        if status:
            raise ReceiverError(f"马达临时模式被拒绝：{status}")
        self.configuration_count += 1


def waveform_report(left, right, trigger, *, selector=3, trigger_enabled=False,
                    grips_enabled=True):
    """Eight samples at 1 kHz. The trigger column is routed by selector 0..3."""
    if selector not in (0, 1, 2, 3) or any(len(lane) != 8 for lane in (left, right, trigger)):
        raise ValueError("Motor blocks need eight samples and a valid selector")
    payload = bytearray((0x80 | selector | (4 if trigger_enabled else 0)
                         | (0x18 if grips_enabled else 0),))
    for row in zip(trigger, left, right):
        for index, sample in enumerate(row):
            if isinstance(sample, bool) or not isinstance(sample, int) or not -127 <= sample <= 127:
                raise ValueError("Motor samples must be signed integers in -127..127")
            enabled = trigger_enabled if index == 0 else grips_enabled
            payload.append(sample + 128 if enabled else 128)
    return command_report(0x57, payload)
