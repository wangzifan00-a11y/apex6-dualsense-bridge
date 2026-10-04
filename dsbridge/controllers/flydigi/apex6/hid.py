"""APEX 6 receiver HID discovery and bounded I/O; construction sends nothing."""
from __future__ import annotations

import ctypes as C

from dsbridge.diagnostics.sony_writer import (_Api, DWORD, GUID, HANDLE, SP_DEVICE_INTERFACE_DATA,
                              SP_DEVINFO_DATA, INVALID_HANDLE_VALUE)

VID, PID, USAGE_PAGE = 0x37D7, 0x2502, 0xFFA0


class ReceiverError(RuntimeError):
    pass


def receiver_interfaces():
    api = _Api()
    guid = GUID()
    api.hid.HidD_GetHidGuid(C.byref(guid))
    info = api.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
    if info in (None, INVALID_HANDLE_VALUE):
        raise ReceiverError("无法枚举接收器 HID 接口")
    rows = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA(cbSize=C.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not api.setup.SetupDiEnumDeviceInterfaces(info, None, C.byref(guid), index, C.byref(interface)):
                if C.get_last_error() == 259:
                    break
                raise ReceiverError(f"HID 枚举错误：{C.get_last_error()}")
            index += 1
            needed = DWORD()
            api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), None, 0, C.byref(needed), None)
            if not 8 <= needed.value <= 65536:
                continue
            detail = C.create_string_buffer(needed.value)
            DWORD.from_buffer(detail).value = 8 if C.sizeof(C.c_void_p) == 8 else 6
            dev = SP_DEVINFO_DATA(cbSize=C.sizeof(SP_DEVINFO_DATA))
            if not api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), detail, needed,
                                                             C.byref(needed), C.byref(dev)):
                continue
            path = C.wstring_at(C.addressof(detail) + 4)
            if "vid_37d7&pid_2502" not in path.lower():
                continue
            instance = C.create_unicode_buffer(512)
            api.cfg.CM_Get_Device_IDW(dev.DevInst, instance, len(instance), 0)
            row = {"path": path, "instance_id": instance.value}
            handle = None
            try:
                handle = api.open(path)
                attrs, caps = api.inspect(handle)
                row.update(vendor_id=attrs.VendorID, product_id=attrs.ProductID,
                           usage_page=caps.UsagePage, usage=caps.Usage,
                           input_length=caps.InputReportByteLength,
                           output_length=caps.OutputReportByteLength,
                           feature_length=caps.FeatureReportByteLength, readable=True)
            except Exception as exc:
                row.update(readable=False, error=str(exc))
            finally:
                if handle not in (None, INVALID_HANDLE_VALUE):
                    api.kernel.CloseHandle(handle)
            rows.append(row)
    finally:
        api.setup.SetupDiDestroyDeviceInfoList(info)
    return rows


class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


class ReceiverHID:
    """One explicitly selected vendor interface. All I/O has a deadline."""
    def __init__(self, path):
        self.api = _Api()
        self.handle = None
        self.path = path
        self.api.hid.HidD_FlushQueue.argtypes = [HANDLE]
        self.api.hid.HidD_FlushQueue.restype = C.c_ubyte
        kernel = self.api.kernel
        for name, args, result in (
            ("CreateEventW", [C.c_void_p, C.c_int, C.c_int, C.c_wchar_p], HANDLE),
            ("WaitForSingleObject", [HANDLE, DWORD], DWORD),
            ("GetOverlappedResult", [HANDLE, C.POINTER(OVERLAPPED), C.POINTER(DWORD), C.c_int], C.c_int),
            ("CancelIoEx", [HANDLE, C.POINTER(OVERLAPPED)], C.c_int),
            ("ReadFile", [HANDLE, C.c_void_p, DWORD, C.POINTER(DWORD), C.c_void_p], C.c_int),
        ):
            method = getattr(kernel, name)
            method.argtypes, method.restype = args, result
        if "vid_37d7&pid_2502" not in str(path).lower():
            raise ReceiverError("仅允许已选择的 APEX 6 接收器接口")
        self.handle = kernel.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)
        if self.handle in (None, INVALID_HANDLE_VALUE):
            self.handle = None
            raise ReceiverError(f"无法打开接收器输出接口：{C.get_last_error()}")
        try:
            attrs, caps = self.api.inspect(self.handle)
            if (attrs.VendorID, attrs.ProductID, caps.UsagePage, caps.OutputReportByteLength) != (VID, PID, USAGE_PAGE, 33):
                raise ReceiverError("接收器输出接口与已核实的 APEX 6 协议不匹配")
            if caps.InputReportByteLength != 33:
                raise ReceiverError("接收器应答报告长度不匹配")
        except Exception:
            self.close()
            raise

    def _io(self, writing, payload=None, timeout_ms=500):
        if not self.handle:
            raise ReceiverError("接收器接口已关闭")
        if writing and len(payload) != 33:
            raise ValueError("APEX 6 输出报告必须为 33 字节")
        buffer = C.create_string_buffer(payload, 33) if writing else C.create_string_buffer(33)
        event = self.api.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise ReceiverError("无法建立接收器 I/O 事件")
        overlapped = OVERLAPPED(hEvent=event)
        transferred = DWORD()
        try:
            method = self.api.kernel.WriteFile if writing else self.api.kernel.ReadFile
            result = method(self.handle, buffer, 33, C.byref(transferred), C.byref(overlapped))
            if not result:
                error = C.get_last_error()
                if error != 997:
                    raise ReceiverError(f"接收器 {'写入' if writing else '读取'}失败：{error}")
                ready = self.api.kernel.WaitForSingleObject(event, timeout_ms)
                if ready != 0:
                    self.api.kernel.CancelIoEx(self.handle, C.byref(overlapped))
                    # Reap cancellation before freeing its buffer or OVERLAPPED.
                    self.api.kernel.GetOverlappedResult(self.handle, C.byref(overlapped), C.byref(transferred), True)
                    if ready == 258:
                        if writing:
                            raise ReceiverError("接收器写入超时")
                        return None
                    raise ReceiverError(f"接收器 I/O 等待失败：{ready}")
                if not self.api.kernel.GetOverlappedResult(self.handle, C.byref(overlapped), C.byref(transferred), False):
                    raise ReceiverError(f"接收器 I/O 完成失败：{C.get_last_error()}")
            if transferred.value != 33:
                raise ReceiverError(f"接收器报告不完整：{transferred.value}/33")
            return bytes(buffer.raw) if not writing else None
        finally:
            self.api.kernel.CloseHandle(event)

    def write(self, payload):
        return self._io(True, payload)

    def read(self, timeout_ms=500):
        return self._io(False, timeout_ms=timeout_ms)

    def flush_queue(self):
        if not self.handle or not self.api.hid.HidD_FlushQueue(self.handle):
            raise ReceiverError(f"无法清理旧的接收器控制应答：{C.get_last_error()}")

    def close(self):
        if self.handle:
            self.api.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
