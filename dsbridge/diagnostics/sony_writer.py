"""Explicit, opt-in HID writer for this project's USB/IP virtual DualSense.

No reports are sent on import or enumeration. There is no device creation,
driver installation, private Flydigi command, or automatic controller choice.
send_rumble() requires an explicit path and verifies USB/IP ancestry again.
The caller owns the test duration and must send (0, 0) in its finally block.

Sources checked against hbashton/VIIPER v0.1.9-rc4.6.6:
https://github.com/hbashton/VIIPER/blob/v0.1.9-rc4.6.6/device/dualsense/device.go
  USB output report ID 0x02; validity selectors byte 1; small motor byte 3,
  large motor byte 4. Flag 0x03 selects ordinary compatible vibration.
https://github.com/hbashton/VIIPER/blob/v0.1.9-rc4.6.6/device/dualsense/const.go
  VID 0x054C, PID 0x0CE6, native USB output report length 48.
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/hidpi/ns-hidpi-_hidp_caps
https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/sending-hid-reports

USB/IP virtual ancestry is identified through the installed usbip2_ude service.
VID/PID alone would also match physical Sony devices and is insufficient.
"""

from __future__ import annotations

import ctypes as C
import sys


class SonyWriterError(RuntimeError):
    pass


DWORD = C.c_uint32
USHORT = C.c_uint16
HANDLE = C.c_void_p
ULONG_PTR = C.c_size_t
INVALID_HANDLE_VALUE = C.c_void_p(-1).value


class GUID(C.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", USHORT), ("Data3", USHORT),
                ("Data4", C.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(C.Structure):
    _fields_ = [("cbSize", DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", DWORD), ("Reserved", ULONG_PTR)]


class SP_DEVINFO_DATA(C.Structure):
    _fields_ = [("cbSize", DWORD), ("ClassGuid", GUID),
                ("DevInst", DWORD), ("Reserved", ULONG_PTR)]


class HIDD_ATTRIBUTES(C.Structure):
    _fields_ = [("Size", DWORD), ("VendorID", USHORT),
                ("ProductID", USHORT), ("VersionNumber", USHORT)]


class HIDP_CAPS(C.Structure):
    _fields_ = [("Usage", USHORT), ("UsagePage", USHORT),
                ("InputReportByteLength", USHORT),
                ("OutputReportByteLength", USHORT),
                ("FeatureReportByteLength", USHORT), ("Reserved", USHORT * 17),
                ("NumberLinkCollectionNodes", USHORT),
                ("NumberInputButtonCaps", USHORT), ("NumberInputValueCaps", USHORT),
                ("NumberInputDataIndices", USHORT), ("NumberOutputButtonCaps", USHORT),
                ("NumberOutputValueCaps", USHORT), ("NumberOutputDataIndices", USHORT),
                ("NumberFeatureButtonCaps", USHORT), ("NumberFeatureValueCaps", USHORT),
                ("NumberFeatureDataIndices", USHORT)]


class _Api:
    def __init__(self):
        if sys.platform != "win32":
            raise SonyWriterError("Sony HID test writer requires Windows")
        self.hid = C.WinDLL("hid.dll", use_last_error=True)
        self.setup = C.WinDLL("setupapi.dll", use_last_error=True)
        self.cfg = C.WinDLL("cfgmgr32.dll", use_last_error=True)
        self.kernel = C.WinDLL("kernel32.dll", use_last_error=True)
        specs = (
            (self.hid, "HidD_GetHidGuid", [C.POINTER(GUID)], None),
            (self.hid, "HidD_GetAttributes", [HANDLE, C.POINTER(HIDD_ATTRIBUTES)], C.c_ubyte),
            (self.hid, "HidD_GetPreparsedData", [HANDLE, C.POINTER(C.c_void_p)], C.c_ubyte),
            (self.hid, "HidD_FreePreparsedData", [C.c_void_p], C.c_ubyte),
            (self.hid, "HidP_GetCaps", [C.c_void_p, C.POINTER(HIDP_CAPS)], C.c_int32),
            (self.setup, "SetupDiGetClassDevsW", [C.POINTER(GUID), C.c_wchar_p, HANDLE, DWORD], HANDLE),
            (self.setup, "SetupDiEnumDeviceInterfaces", [HANDLE, C.c_void_p, C.POINTER(GUID), DWORD,
                                                         C.POINTER(SP_DEVICE_INTERFACE_DATA)], C.c_int),
            (self.setup, "SetupDiGetDeviceInterfaceDetailW", [HANDLE, C.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                              C.c_void_p, DWORD, C.POINTER(DWORD),
                                                              C.POINTER(SP_DEVINFO_DATA)], C.c_int),
            (self.setup, "SetupDiDestroyDeviceInfoList", [HANDLE], C.c_int),
            (self.cfg, "CM_Get_Parent", [C.POINTER(DWORD), DWORD, DWORD], DWORD),
            (self.cfg, "CM_Get_DevNode_Registry_PropertyW", [DWORD, DWORD, C.POINTER(DWORD),
                                                              C.c_void_p, C.POINTER(DWORD), DWORD], DWORD),
            (self.cfg, "CM_Get_Device_IDW", [DWORD, C.c_wchar_p, DWORD, DWORD], DWORD),
            (self.kernel, "CreateFileW", [C.c_wchar_p, DWORD, DWORD, C.c_void_p, DWORD, DWORD, HANDLE], HANDLE),
            (self.kernel, "WriteFile", [HANDLE, C.c_void_p, DWORD, C.POINTER(DWORD), C.c_void_p], C.c_int),
            (self.kernel, "CloseHandle", [HANDLE], C.c_int),
        )
        for dll, name, args, result in specs:
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

    def open(self, path, *, write=False):
        handle = self.kernel.CreateFileW(path, 0x40000000 if write else 0,
                                         0x01 | 0x02, None, 3, 0, None)
        if handle == INVALID_HANDLE_VALUE or handle is None:
            raise SonyWriterError(f"CreateFileW failed ({C.get_last_error()}) for selected virtual HID")
        return handle

    def inspect(self, handle):
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = C.sizeof(attrs)
        if not self.hid.HidD_GetAttributes(handle, C.byref(attrs)):
            raise SonyWriterError("Cannot read selected HID attributes")
        preparsed = C.c_void_p()
        if not self.hid.HidD_GetPreparsedData(handle, C.byref(preparsed)):
            raise SonyWriterError("Cannot read selected HID preparsed data")
        try:
            caps = HIDP_CAPS()
            if self.hid.HidP_GetCaps(preparsed, C.byref(caps)) != 0x00110000:
                raise SonyWriterError("Cannot read selected HID capabilities")
        finally:
            self.hid.HidD_FreePreparsedData(preparsed)
        return attrs, caps

    def usbip_ancestry(self, devinst):
        """Read-only parent walk; fail closed if a virtual parent cannot be proved."""
        ancestors = []
        node = DWORD(devinst)
        for _ in range(16):
            device_id = C.create_unicode_buffer(512)
            self.cfg.CM_Get_Device_IDW(node, device_id, len(device_id), 0)
            service = C.create_unicode_buffer(256)
            size, kind = DWORD(C.sizeof(service)), DWORD()
            result = self.cfg.CM_Get_DevNode_Registry_PropertyW(
                node, 5, C.byref(kind), service, C.byref(size), 0)  # CM_DRP_SERVICE
            name = service.value if result == 0 else ""
            ancestors.append({"instance_id": device_id.value, "service": name})
            if name.lower() == "usbip2_ude":
                return ancestors
            parent = DWORD()
            if self.cfg.CM_Get_Parent(C.byref(parent), node, 0) != 0:
                break
            node = parent
        return None


def _matching(attrs, caps):
    return (attrs.VendorID == 0x054C and attrs.ProductID == 0x0CE6
            and caps.UsagePage == 1 and caps.Usage == 5
            and caps.InputReportByteLength == 64
            and caps.OutputReportByteLength == 48)


def describe_dualsense_outputs() -> list[dict]:
    """Enumerate readable USB/IP virtual DualSense HID outputs, without writing."""
    api = _Api()
    guid = GUID()
    api.hid.HidD_GetHidGuid(C.byref(guid))
    info = api.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x02 | 0x10)
    if info == INVALID_HANDLE_VALUE or info is None:
        raise SonyWriterError(f"HID enumeration failed ({C.get_last_error()})")
    found = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = C.sizeof(interface)
            if not api.setup.SetupDiEnumDeviceInterfaces(info, None, C.byref(guid), index, C.byref(interface)):
                if C.get_last_error() == 259:  # ERROR_NO_MORE_ITEMS
                    break
                raise SonyWriterError(f"HID enumeration failed at index {index} ({C.get_last_error()})")
            index += 1
            required = DWORD()
            api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), None, 0,
                                                      C.byref(required), None)
            if not 8 <= required.value <= 65536:
                continue
            detail = C.create_string_buffer(required.value)
            DWORD.from_buffer(detail).value = 8 if C.sizeof(C.c_void_p) == 8 else 6
            devinfo = SP_DEVINFO_DATA()
            devinfo.cbSize = C.sizeof(devinfo)
            if not api.setup.SetupDiGetDeviceInterfaceDetailW(
                    info, C.byref(interface), detail, required, C.byref(required), C.byref(devinfo)):
                continue
            path = C.wstring_at(C.addressof(detail) + 4)
            if "vid_054c&pid_0ce6" not in path.lower():
                continue
            try:
                handle = api.open(path)
                try:
                    attrs, caps = api.inspect(handle)
                finally:
                    api.kernel.CloseHandle(handle)
            except SonyWriterError:
                continue
            if not _matching(attrs, caps):
                continue
            ancestors = api.usbip_ancestry(devinfo.DevInst)
            if ancestors is None:
                continue
            found.append({"path": path, "vendor_id": attrs.VendorID,
                          "product_id": attrs.ProductID, "usage_page": caps.UsagePage,
                          "usage": caps.Usage, "output_length": caps.OutputReportByteLength,
                          "input_length": caps.InputReportByteLength,
                          "virtual_ancestry": ancestors})
    finally:
        api.setup.SetupDiDestroyDeviceInfoList(info)
    return found


def enum_dualsense_outputs() -> list[str]:
    """Return explicit selectable paths; never pick among multiple controllers."""
    return [device["path"] for device in describe_dualsense_outputs()]


def build_rumble_report(leftbyte: int, rightbyte: int, output_length: int = 48) -> bytes:
    """Build an ordinary USB rumble update; contains no trigger/LED effects."""
    for value in (leftbyte, rightbyte):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError("Motor strength must be an integer in 0..255")
    if output_length != 48:
        raise SonyWriterError("Only the verified 48-byte DualSense USB output report is supported")
    report = bytearray(output_length)
    report[0] = 0x02
    report[1] = 0x03
    report[3] = rightbyte
    report[4] = leftbyte
    return bytes(report)


def _send_virtual_report(path: str, report: bytes) -> dict:
    """Write exactly one report to an explicitly selected USB/IP virtual HID.

    The original game/authored waveform cannot be tested through this helper;
    it exercises Sony ordinary rumble -> VIIPER feedback -> bridge motors.
    Synchronous WriteFile is used; run test sequences in a bounded subprocess.
    """
    if not isinstance(path, str) or not path:
        raise SonyWriterError("An explicit virtual DualSense HID path is required")
    devices = [item for item in describe_dualsense_outputs() if item["path"].lower() == path.lower()]
    if len(devices) != 1:
        raise SonyWriterError("Selected path is not a uniquely verified USB/IP virtual DualSense HID output")
    selected = devices[0]
    if len(report) != selected["output_length"] or report[0] != 2:
        raise SonyWriterError("Only a complete verified virtual Sony USB report is writable")
    api = _Api()
    handle = api.open(selected["path"], write=True)
    try:
        attrs, caps = api.inspect(handle)
        if not _matching(attrs, caps):
            raise SonyWriterError("Selected virtual HID identity changed before write")
        buffer = C.create_string_buffer(report, len(report))
        written = DWORD()
        if not api.kernel.WriteFile(handle, buffer, len(report), C.byref(written), None):
            raise SonyWriterError(f"Virtual DualSense WriteFile failed ({C.get_last_error()})")
        if written.value != len(report):
            raise SonyWriterError(f"Virtual DualSense short HID write: {written.value}/{len(report)}")
    finally:
        api.kernel.CloseHandle(handle)
    return {"path": selected["path"], "bytes_written": written.value}


def send_rumble(path: str, leftbyte: int, rightbyte: int) -> dict:
    result = _send_virtual_report(path, build_rumble_report(leftbyte, rightbyte))
    result.update({
            "left_large": leftbyte, "right_small": rightbyte,
            "source": "synthetic Sony USB ordinary rumble test"})
    return result


def send_trigger_effects(path: str, left: bytes, right: bytes) -> dict:
    """Synthetic trigger roundtrip, restricted to USB/IP virtual DS ancestry."""
    if len(left) != 11 or len(right) != 11:
        raise ValueError("Trigger effects must be eleven bytes each")
    report = bytearray(48)
    report[0], report[1] = 2, 0x0c
    report[11:22], report[22:33] = right, left
    result = _send_virtual_report(path, bytes(report))
    result["source"] = "synthetic Sony USB adaptive-trigger test"
    return result


if __name__ == "__main__":
    # CLI is read-only. Actual output always requires explicit Python invocation.
    import json
    print(json.dumps(describe_dualsense_outputs(), indent=2, ensure_ascii=False))
