"""Read HidHide's documented control API, preserving Unicode paths. No SET requests.

Codes and wire format: nefarius/HidHide v1.5.230.0,
HidHideCLI/src/FilterDriverProxy.cpp and the official API documentation.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path


def read_config():
    native = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    native.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    native.CreateFileW.restype = wintypes.HANDLE
    native.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    native.DeviceIoControl.restype = wintypes.BOOL
    native.CloseHandle.argtypes = [wintypes.HANDLE]
    native.CloseHandle.restype = wintypes.BOOL
    native.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    native.QueryDosDeviceW.restype = wintypes.DWORD
    handle = native.CreateFileW(r'\\.\HidHide', 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def get(function, size=0):
        # CTL_CODE(32769, function, METHOD_BUFFERED, FILE_READ_DATA).
        code = (32769 << 16) | (1 << 14) | (function << 2)
        buffer = ctypes.create_string_buffer(size) if size else None
        needed = wintypes.DWORD()
        if not native.DeviceIoControl(handle, code, None, 0, buffer, size, ctypes.byref(needed), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer, needed.value

    def strings(function):
        _, needed = get(function)
        if needed > 1024 * 1024:
            raise RuntimeError('Unexpected HidHide configuration size')
        buffer, used = get(function, max(4, needed * 2))
        return [entry for entry in buffer.raw[:used].decode('utf-16-le').split('\0') if entry]

    try:
        allowed_nt = strings(2048)
        hidden = strings(2050)
        active_buffer, active_size = get(2052, 1)
        inverse_buffer, inverse_size = get(2054, 1)
        if active_size != 1 or inverse_size != 1:
            raise RuntimeError('Unexpected HidHide boolean size')
        active = bool(active_buffer.raw[0])
        inverse = bool(inverse_buffer.raw[0])
    finally:
        native.CloseHandle(handle)
    # The API returns NT image paths; map volume names back to ordinary drive paths.
    volumes = []
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        drive = letter + ':'
        target = ctypes.create_unicode_buffer(32768)
        if native.QueryDosDeviceW(drive, target, len(target)):
            volumes.append((target.value, drive))
    allowed = []
    for entry in allowed_nt:
        for target, drive in volumes:
            if entry.casefold().startswith((target + '\\').casefold()):
                allowed.append(drive + entry[len(target):])
                break
        else:
            allowed.append(entry)
    return {'allowed_apps': allowed, 'hidden_devices': hidden, 'active': active,
            'inverse': inverse, 'configuration_write_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    output = json.dumps(read_config(), ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(output, encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
