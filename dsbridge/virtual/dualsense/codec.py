"""Pure VPCM V5 / Sony input and haptic codec; no device or network I/O."""
import math
import struct
import zlib
from collections.abc import Mapping

DEVICE_TYPE = "dualsensecombinedaudioduplexv5"
FEEDBACK_SIZE = 474
ATOMIC_SIZE = 2 + FEEDBACK_SIZE + 1920
_HEADER = struct.Struct("<4sBBHII")
_INPUT = struct.Struct("<4bIBBBHHBHHB6h")
_FEEDBACK_LENGTHS = {0x81: FEEDBACK_SIZE, 0x83: ATOMIC_SIZE,
                     0x84: FEEDBACK_SIZE}
_BUTTON_MAP = ((0x1000, 0x20), (0x2000, 0x40), (0x4000, 0x10),
               (0x8000, 0x80), (0x0100, 0x100), (0x0200, 0x200),
               (0x0020, 0x1000), (0x0010, 0x2000),
               (0x0040, 0x4000), (0x0080, 0x8000), (0x0400, 0x10000))


class ViiperError(RuntimeError):
    """VIIPER availability, ownership, stream or protocol error."""


class ProtocolError(ViiperError):
    """Unsupported or malformed feedback; no guessed decoding is performed."""


def encode_frame(frame_type: int, payload: bytes, sequence: int) -> bytes:
    """Encode the exact 16-byte VPCM version-5 header and IEEE CRC32."""
    if not 0 <= frame_type <= 255 or len(payload) > 65535:
        raise ValueError("Invalid VIIPER frame type or size")
    fields = struct.pack("<BBHI", 5, frame_type, len(payload), sequence & 0xFFFFFFFF)
    crc = zlib.crc32(payload, zlib.crc32(fields)) & 0xFFFFFFFF
    return b"VPCM" + fields + struct.pack("<I", crc) + payload


class FrameDecoder:
    """Strict incremental receiver: TCP fragmentation is not packet framing."""

    def __init__(self, *, events: bool = False):
        self.buffer = bytearray()
        self.expected_sequence = 0
        self.lengths = dict(_FEEDBACK_LENGTHS)
        if events:
            self.lengths[0x85] = 9

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= _HEADER.size:
            magic, version, kind, length, sequence, crc = _HEADER.unpack_from(self.buffer)
            if magic != b"VPCM" or version != 5:
                raise ProtocolError("VIIPER 反馈不是已核实的 VPCM V5 协议，请使用匹配的 hbashton VIIPER。")
            if self.lengths.get(kind) != length:
                raise ProtocolError(f"VIIPER 未知反馈类型/长度: 0x{kind:02X}/{length}。已停止震动。")
            if sequence != self.expected_sequence:
                raise ProtocolError(f"VIIPER 反馈序号错误: {sequence}，应为 {self.expected_sequence}。")
            if len(self.buffer) < _HEADER.size + length:
                break
            payload = bytes(self.buffer[_HEADER.size:_HEADER.size + length])
            calculated = zlib.crc32(payload, zlib.crc32(self.buffer[4:12])) & 0xFFFFFFFF
            if crc != calculated:
                raise ProtocolError("VIIPER 反馈 CRC32 校验失败。已停止震动。")
            del self.buffer[:_HEADER.size + length]
            self.expected_sequence = (sequence + 1) & 0xFFFFFFFF
            result.append((kind, payload))
        return result


def _integer(state: Mapping, key: str, low: int, high: int) -> int:
    value = state.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer in {low}..{high}")
    return max(low, min(high, value))


def encode_input(state: Mapping) -> bytes:
    """Map raw XInput axes/buttons to VIIPER's 33-byte DualSense state.

    buttons is XInput wButtons; triggers are 0..255, axes -32768..32767.
    Guide is accepted when supplied as XInput's undocumented 0x0400 bit.
    Unknown Xbox paddles cannot be read through standard XInput.
    """
    if not isinstance(state, Mapping):
        raise ValueError("XInput state must be a mapping")
    raw_buttons = _integer(state, "buttons", 0, 0xFFFF)
    buttons = sum(sony for xbox, sony in _BUTTON_MAP if raw_buttons & xbox)
    lt = _integer(state, "left_trigger", 0, 255)
    rt = _integer(state, "right_trigger", 0, 255)
    if lt:
        buttons |= 0x400
    if rt:
        buttons |= 0x800
    dpad = sum(sony for xbox, sony in ((1, 1), (2, 2), (4, 4), (8, 8))
               if raw_buttons & xbox)
    axes = []
    for key in ("lx", "ly", "rx", "ry"):
        value = _integer(state, key, -32768, 32767)
        # Signed XInput Y increases upward; the Sony report increases downward.
        if key in ("ly", "ry"):
            value = -value
        axes.append(max(-128, min(127, round(value / 256))))
    # Touch contacts inactive, IMU resting flat; do not fabricate physical data.
    return _INPUT.pack(*axes, buttons, dpad, lt, rt,
                       0, 0, 0x80, 0, 0, 0x80, 0, 0, 0, 0, 0, -8192)


def decode_haptic_pcm(feedback: bytes) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """Decode only the V5 rear-channel 3 kHz signed-8-bit stereo PCM lane.

    The 1920-byte front/speaker tail of frame 0x83 is deliberately excluded.
    No audio-device loopback, game soundtrack or speech enters this function.
    """
    if len(feedback) != FEEDBACK_SIZE:
        raise ProtocolError("VIIPER DualSense feedback must contain exactly 474 bytes")
    carrier = feedback[76:]
    if not any(carrier):
        return None  # Ordinary HID control update, without time-bearing media.
    if (carrier[0] != 0x36 or carrier[2:5] != b"\x91\x07\xfe"
            or carrier[11:13] != b"\x90\x3f"
            or carrier[76:78] != b"\x92\x40"
            or carrier[142] != 0x93 or carrier[143] not in (0, 200)):
        raise ProtocolError("VIIPER 原生触觉载荷不符合已核实的 DualSense 0x36 格式。")
    crc = struct.unpack_from("<I", carrier, 394)[0]
    if crc != zlib.crc32(carrier[:394], 0xEADA2D49) & 0xFFFFFFFF:
        raise ProtocolError("DualSense 0x36 触觉载荷 CRC32 校验失败。")
    samples = struct.unpack_from("<64b", carrier, 78)
    return tuple(samples[lane::2] for lane in (0, 1))


def decode_haptics(feedback: bytes) -> tuple[float, float] | None:
    """Keep the verified Bluetooth strength conversion unchanged."""
    pcm = decode_haptic_pcm(feedback)
    if pcm is None:
        return None
    return tuple(math.sqrt(sum(v * v for v in lane) / 32) / 128 for lane in pcm)


