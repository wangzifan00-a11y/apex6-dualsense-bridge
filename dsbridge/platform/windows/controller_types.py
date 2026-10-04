"""Native report ABI and pure normalized-input conversions."""
from __future__ import annotations
import ctypes
import math
from typing import Any, Mapping

class BackendError(RuntimeError):
    """A driver or native API could not safely complete an operation."""


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_uint16),
        ("bLeftTrigger", ctypes.c_uint8),
        ("bRightTrigger", ctypes.c_uint8),
        ("sThumbLX", ctypes.c_int16),
        ("sThumbLY", ctypes.c_int16),
        ("sThumbRX", ctypes.c_int16),
        ("sThumbRY", ctypes.c_int16),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_uint16), ("wRightMotorSpeed", ctypes.c_uint16)]


class DS4_REPORT(ctypes.Structure):
    # The native C ABI has nine data bytes followed by one alignment byte.
    # Do not pack this to 1: vigem_target_ds4_update takes the struct by value.
    _fields_ = [
        ("bThumbLX", ctypes.c_uint8),
        ("bThumbLY", ctypes.c_uint8),
        ("bThumbRX", ctypes.c_uint8),
        ("bThumbRY", ctypes.c_uint8),
        ("wButtons", ctypes.c_uint16),
        ("bSpecial", ctypes.c_uint8),
        ("bTriggerL", ctypes.c_uint8),
        ("bTriggerR", ctypes.c_uint8),
    ]


class DS4_LIGHTBAR_COLOR(ctypes.Structure):
    _fields_ = [("Red", ctypes.c_uint8), ("Green", ctypes.c_uint8), ("Blue", ctypes.c_uint8)]


# A DS4 callback's fifth argument is an RGB structure, not an X360 LED byte.
_CALLBACK_FACTORY = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
DS4_NOTIFICATION = _CALLBACK_FACTORY(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint8,
    ctypes.c_uint8,
    DS4_LIGHTBAR_COLOR,
    ctypes.c_void_p,
)

X360_NOTIFICATION = _CALLBACK_FACTORY(
    None, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_void_p,
)


_BUTTON_MAP = (
    (0x1000, 1 << 5),  # A -> Cross
    (0x2000, 1 << 6),  # B -> Circle
    (0x4000, 1 << 4),  # X -> Square
    (0x8000, 1 << 7),  # Y -> Triangle
    (0x0100, 1 << 8),  # LB -> L1
    (0x0200, 1 << 9),  # RB -> R1
    (0x0020, 1 << 12),  # Back -> Share
    (0x0010, 1 << 13),  # Start -> Options
    (0x0040, 1 << 14),  # Left stick -> L3
    (0x0080, 1 << 15),  # Right stick -> R3
)


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Controller report values must be raw integers")
    return min(maximum, max(minimum, value))


def axis_to_byte(value: int, invert: bool = False) -> int:
    """Map XInput's asymmetric signed axis to DS4's 0..255, center 128."""
    value = _integer(value, -32768, 32767)
    if invert:
        value = min(32767, max(-32768, -value))
    return ((value + 32768) * 255 + 32767) // 65535


def _dpad(buttons: int) -> int:
    # Opposite directions cancel, including invalid states from other callers.
    x = int(bool(buttons & 0x0008)) - int(bool(buttons & 0x0004))
    y = int(bool(buttons & 0x0001)) - int(bool(buttons & 0x0002))
    return {
        (0, 0): 8, (0, 1): 0, (1, 1): 1, (1, 0): 2,
        (1, -1): 3, (0, -1): 4, (-1, -1): 5, (-1, 0): 6, (-1, 1): 7,
    }[(x, y)]


def xbox_to_ds4(state: Mapping[str, int]) -> DS4_REPORT:
    """Translate one raw XInput state without accessing hardware."""
    buttons = _integer(state.get("buttons", 0), 0, 0xFFFF)
    left_trigger = _integer(state.get("left_trigger", 0), 0, 255)
    right_trigger = _integer(state.get("right_trigger", 0), 0, 255)
    ds_buttons = _dpad(buttons)
    for xbox_mask, ds_mask in _BUTTON_MAP:
        if buttons & xbox_mask:
            ds_buttons |= ds_mask
    # XInput's documented digital trigger threshold is 30.
    if left_trigger > 30:
        ds_buttons |= 1 << 10
    if right_trigger > 30:
        ds_buttons |= 1 << 11
    return DS4_REPORT(
        bThumbLX=axis_to_byte(state.get("lx", 0)),
        bThumbLY=axis_to_byte(state.get("ly", 0), invert=True),
        bThumbRX=axis_to_byte(state.get("rx", 0)),
        bThumbRY=axis_to_byte(state.get("ry", 0), invert=True),
        wButtons=ds_buttons,
        bSpecial=int(bool(buttons & 0x0400)),
        bTriggerL=left_trigger,
        bTriggerR=right_trigger,
    )


def motor_word(value: float) -> int:
    """Clamp a normalized motor level and reject NaN/infinity."""
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Motor level must be finite")
    return round(min(1.0, max(0.0, value)) * 65535)


def _slot(index: int) -> int:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 3:
        raise ValueError("XInput slot must be an integer from 0 to 3")
    return index


