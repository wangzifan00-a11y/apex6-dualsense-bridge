"""Convert validated Sony reports into controller-neutral feedback events."""
import math
from dsbridge.core.feedback import SonyFeedback, TriggerEffect, TriggerUpdate
from dsbridge.virtual.dualsense.codec import ProtocolError, decode_haptic_pcm

def decode_trigger(data):
    """Eleven-byte DS USB effect; unknown/malformed modes stop only that lane."""
    if len(data) != 11:
        raise ValueError("DS 扳机反馈长度无效")
    mode = data[0]
    if mode in (0, 5):
        return TriggerEffect()
    if mode in (0x21, 0x25, 0x26):
        mask = int.from_bytes(data[1:3], "little")
        if mask & ~0x3ff:
            raise ValueError("DS 扳机区域掩码无效")
        if not mask or (mode == 0x26 and not data[9]):
            return TriggerEffect()
        zones = [i for i in range(10) if mask & (1 << i)]
        if mode == 0x25:
            if len(zones) != 2:
                raise ValueError("DS 武器扳机需要两个触发位置")
            return TriggerEffect(kind="weapon", start=math.ceil(zones[0] * 256 / 10),
                                 end=math.ceil(zones[1] * 256 / 10), strength=(data[3] & 7) + 1)
        packed = int.from_bytes(data[3:7], "little")
        levels = tuple(((packed >> (3 * i)) & 7) + 1 if i in zones else 0 for i in range(10))
        return TriggerEffect(kind="vibration" if mode == 0x26 else "texture",
                             strengths=levels, frequency=data[9] if mode == 0x26 else 85)
    if mode == 1:  # Legacy continuous resistance, mapped to bounded texture.
        if not data[2]:
            return TriggerEffect()
        return TriggerEffect(kind="legacy", start=data[1], strength=data[2], frequency=85)
    if mode == 2:  # Legacy section resistance / weapon break.
        if data[1] >= data[2] or not data[3]:
            return TriggerEffect()
        return TriggerEffect(kind="weapon", start=data[1], end=data[2], strength=max(1, round(data[3] / 32)))
    if mode == 6:  # Legacy vibration: frequency, amplitude, start position.
        if not data[1] or not data[2]:
            return TriggerEffect()
        return TriggerEffect(kind="legacy", start=data[3], strength=data[2], frequency=data[1])
    raise ValueError(f"尚未支持的 DS 扳机效果 0x{mode:02X}")


def decode_feedback(kind, payload, at):
    if kind == 0x81:
        raw = payload[28:76]
        if len(raw) != 48 or raw[0] != 2:
            return None
        rumble = None
        if raw[1] & 3 or raw[39] & 0x0c:
            compatible = bool(raw[1] & 1 or raw[39] & 4)
            rumble = (payload[1] / 255, payload[0] / 255) if compatible else (0., 0.)
        triggers = []
        for lane, mask, offset in ((0, 8, 22), (1, 4, 11)):
            if raw[1] & mask:
                data = raw[offset:offset + 11]
                error = None
                try:
                    effect = decode_trigger(data)
                except ValueError as exc:
                    effect, error = TriggerEffect(), str(exc)
                triggers.append(TriggerUpdate(lane, effect, data[0], data, error))
        return SonyFeedback(at, rumble=rumble, triggers=tuple(triggers))
    if kind in (0x83, 0x84):
        pcm = decode_haptic_pcm(payload)
        if pcm is None:
            raise ProtocolError("原生触觉反馈缺少 PCM 载荷")
        return SonyFeedback(at, pcm=pcm)
    return None
