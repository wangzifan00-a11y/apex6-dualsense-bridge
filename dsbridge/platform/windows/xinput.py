"""Physical XInput and per-thread COM ownership; no virtual controller logic."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import threading
from typing import Any
from dsbridge.platform.windows.controller_types import BackendError, XINPUT_STATE, XINPUT_VIBRATION, motor_word, _slot


class _Capabilities(ctypes.Structure):
    from dsbridge.platform.windows.controller_types import XINPUT_GAMEPAD
    _fields_ = [("type", ctypes.c_uint8), ("subtype", ctypes.c_uint8), ("flags", ctypes.c_uint16),
                ("gamepad", XINPUT_GAMEPAD), ("vibration", XINPUT_VIBRATION)]


class _CapabilitiesEx(ctypes.Structure):
    # Same optional Windows 8+ ABI used by SDL's GetXInputDeviceInfo.
    _fields_ = [("caps", _Capabilities), ("vendor", ctypes.c_uint16), ("product", ctypes.c_uint16),
                ("version", ctypes.c_uint16), ("reserved", ctypes.c_uint16), ("reserved2", ctypes.c_uint32)]

class _ComApartment:
    """Keep COM alive on a polling thread, including Windows' newer XInput."""

    def __init__(self) -> None:
        self._owned = False
        self._owner_thread = threading.get_ident()
        system_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        self._ole = ctypes.WinDLL(str(system_dir / "ole32.dll"))
        self._ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._ole.CoInitializeEx.restype = ctypes.c_int32
        self._ole.CoUninitialize.argtypes = []
        self._ole.CoUninitialize.restype = None
        code = int(self._ole.CoInitializeEx(None, 0)) & 0xFFFFFFFF
        self._owned = code in (0, 1)  # S_OK / S_FALSE both increment COM refcount.
        # RPC_E_CHANGED_MODE means the caller already has a usable STA.
        if not self._owned and code != 0x80010106:
            raise BackendError(f"CoInitializeEx failed: 0x{code:08X}")

    def close(self) -> None:
        if self._owned and self._owner_thread == threading.get_ident():
            self._ole.CoUninitialize()
            self._owned = False

    def __del__(self) -> None:
        self.close()


class XInput:
    """Read four XInput slots; set_rumble is the only output operation."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise BackendError("XInput is only available on Windows")
        self.dll_name = ""
        self._dll = None
        system_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        failures = []
        for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
            try:
                self._dll = ctypes.WinDLL(str(system_dir / name))
                self.dll_name = name
                break
            except OSError as exc:
                failures.append(str(exc))
        if self._dll is None:
            raise BackendError("Could not load a Windows XInput library: " + "; ".join(failures))
        self._thread = threading.local()
        self._get = self._dll.XInputGetState
        self._get.argtypes = [ctypes.c_uint32, ctypes.POINTER(XINPUT_STATE)]
        self._get.restype = ctypes.c_uint32
        self._set = self._dll.XInputSetState
        self._set.argtypes = [ctypes.c_uint32, ctypes.POINTER(XINPUT_VIBRATION)]
        self._set.restype = ctypes.c_uint32

    def _ensure_com(self) -> None:
        # COM belongs to a thread, not the shared XInput object. A worker may
        # differ from the UI thread that constructed this instance.
        if getattr(self._thread, "apartment", None) is None:
            self._thread.apartment = _ComApartment()

    def close(self) -> None:
        """Release COM owned by this instance on the calling polling thread."""
        apartment = getattr(self._thread, "apartment", None)
        if apartment is not None:
            apartment.close()
            self._thread.apartment = None

    def get_state(self, index: int) -> dict[str, int] | None:
        index = _slot(index)
        self._ensure_com()
        native = XINPUT_STATE()
        code = int(self._get(index, ctypes.byref(native)))
        if code == 1167:  # ERROR_DEVICE_NOT_CONNECTED
            return None
        if code != 0:
            raise BackendError(f"XInputGetState({index}) failed: Win32 error {code}")
        pad = native.Gamepad
        return {
            "index": index,
            "packet": native.dwPacketNumber,
            "buttons": pad.wButtons,
            "left_trigger": pad.bLeftTrigger,
            "right_trigger": pad.bRightTrigger,
            "lx": pad.sThumbLX,
            "ly": pad.sThumbLY,
            "rx": pad.sThumbRX,
            "ry": pad.sThumbRY,
        }

    def enumerate(self) -> list[dict[str, int]]:
        return [state for index in range(4) if (state := self.get_state(index)) is not None]

    def diagnostics(self) -> dict[str, Any]:
        """Read connection status only; never send motor commands."""
        return {"dll": self.dll_name, "slots": [
            {"index": index, "connected": self.get_state(index) is not None}
            for index in range(4)
        ]}

    def identity(self, index):
        """Read the selected slot's own identity; never invent fallback VID/PID."""
        index = _slot(index)
        self._ensure_com()
        if self.get_state(index) is None:
            return None
        result = {"slot": index, "vendor_id": None, "product_id": None, "flags": 0}
        try:
            extended = self._dll[108]
        except (AttributeError, OSError):
            extended = None
        if extended:
            extended.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.POINTER(_CapabilitiesEx)]
            extended.restype = ctypes.c_uint32
            value = _CapabilitiesEx()
            if extended(1, index, 0, ctypes.byref(value)) == 0:
                result.update(vendor_id=value.vendor or None, product_id=value.product or None,
                              version=value.version, flags=value.caps.flags)
                return result
        ordinary = self._dll.XInputGetCapabilities
        ordinary.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_Capabilities)]
        ordinary.restype = ctypes.c_uint32
        value = _Capabilities()
        if ordinary(index, 0, ctypes.byref(value)) == 0:
            result["flags"] = value.flags
        return result

    def connection(self, index):
        from dsbridge.platform.windows.connection import detect_connection
        return detect_connection(self.identity(index))

    def set_rumble(self, index: int, left: float, right: float) -> bool:
        index = _slot(index)
        vibration = XINPUT_VIBRATION(motor_word(left), motor_word(right))
        self._ensure_com()
        code = int(self._set(index, ctypes.byref(vibration)))
        if code == 1167:
            return False
        if code != 0:
            raise BackendError(f"XInputSetState({index}) failed: Win32 error {code}")
        return True


