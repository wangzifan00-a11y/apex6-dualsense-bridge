"""Small Unicode-safe HidHide control client, with one handle per transaction.

Uses the official v1.5.230 control API (no administrator elevation required):
https://docs.nefarius.at/projects/HidHide/API-Documentation/
https://github.com/nefarius/HidHide/blob/v1.5.230.0/HidHideCLI/src/FilterDriverProxy.cpp
"""
import ctypes as C
from ctypes import wintypes as W
import time


class HidHideControl:
    def __init__(self):
        self.native = C.WinDLL("kernel32.dll", use_last_error=True)
        for name, args, result in (
            ("CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            ("DeviceIoControl", [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
            ("CloseHandle", [W.HANDLE], W.BOOL),
            ("QueryDosDeviceW", [W.LPCWSTR, W.LPWSTR, W.DWORD], W.DWORD),
        ):
            method = getattr(self.native, name)
            method.argtypes, method.restype = args, result
        self.handle = None

    def __enter__(self):
        deadline = time.monotonic() + 3
        while True:
            self.handle = self.native.CreateFileW(r"\\.\HidHide", 0x80000000, 7, None, 3, 0x80, None)
            if self.handle != C.c_void_p(-1).value:
                return self
            error = C.get_last_error()
            self.handle = None
            if error not in (5, 32) or time.monotonic() >= deadline:
                raise C.WinError(error)
            time.sleep(.05)

    def __exit__(self, *unused):
        if self.handle is not None:
            self.native.CloseHandle(self.handle)
            self.handle = None

    def _io(self, function, payload=None, size=0):
        code = (32769 << 16) | (1 << 14) | (function << 2)
        source = C.create_string_buffer(payload, len(payload)) if payload is not None else None
        output = C.create_string_buffer(size) if size else None
        used = W.DWORD()
        if not self.native.DeviceIoControl(self.handle, code, source, len(payload) if payload is not None else 0,
                                          output, size, C.byref(used), None):
            raise C.WinError(C.get_last_error())
        return output.raw[:used.value] if output is not None else b"", used.value

    def strings(self, function):
        _, size = self._io(function)
        if size > 1024 * 1024:
            raise RuntimeError("HidHide 设置长度异常")
        data, _ = self._io(function, size=max(4, size * 2))
        return [value for value in data.decode("utf-16-le").split("\0") if value]

    def set_strings(self, function, values):
        if any(not value or "\0" in value for value in values):
            raise ValueError("HidHide 路径无效")
        self._io(function, ("\0".join(values) + "\0\0").encode("utf-16-le"))

    def boolean(self, function):
        data, size = self._io(function, size=1)
        if size != 1:
            raise RuntimeError("HidHide 状态长度异常")
        return bool(data[0])

    def snapshot(self):
        return dict(hidden_devices=self.strings(2050), allowed_nt=self.strings(2048),
                    active=self.boolean(2052), inverse=self.boolean(2054))

    def hidden(self, values):
        self.set_strings(2051, values)

    def active(self, value):
        self._io(2053, bytes((bool(value),)))

    def allow(self, executable):
        # HidHide stores NT image names; keep Chinese characters intact.
        path = str(executable)
        if len(path) < 3 or path[1:3] != ":\\":
            raise ValueError("允许列表需要完整的本地程序路径")
        target = C.create_unicode_buffer(32768)
        if not self.native.QueryDosDeviceW(path[:2], target, len(target)):
            raise C.WinError(C.get_last_error())
        nt_path = target.value + path[2:]
        allowed = self.strings(2048)
        if nt_path.casefold() not in {value.casefold() for value in allowed}:
            self.set_strings(2049, allowed + [nt_path])


