"""Read-only evidence of processes holding the selected controller's inputs.

This is NOT a game detector or a proof that an application is polling. XInput
and GameInput may use a system broker, and Raw Input registration is private to
each process. An empty handle scan therefore remains ``unknown``. Only exact
device-object matches are reported; names of executables and loaded DLLs never
create evidence. No handles in another process are closed or changed.

Native name queries can block inside a driver. They run only in a disposable
child process, whose lifetime is bounded by the public probe. A frozen entry
point must dispatch ``--controller-usage-worker REQUEST`` to worker_main.

References:
https://learn.microsoft.com/windows-hardware/drivers/install/devpkey-device-pdoname
https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntqueryobject
https://learn.microsoft.com/windows/win32/api/handleapi/nf-handleapi-duplicatehandle
https://learn.microsoft.com/windows/win32/inputdev/raw-input
"""
from __future__ import annotations

import base64
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import time

_COVERAGE = ["raw_input_registration_is_process_private",
             "xinput_gameinput_brokers_cannot_be_attributed_to_games"]
_VIRTUAL_SERVICES = {"vigembus", "usbip2_ude", "vhidmini", "vjoy"}
_INPUT_SERVICES = {"xusb22", "xusb21", "xusb", "xinputhid", "hidusb", "hidbth", "hidble"}


def _result(clients=(), *, scan_complete=False, reasons=(), scanned=0):
    unique = {}
    for client in clients:
        pid = client.get("pid")
        if type(pid) is not int or pid <= 0:
            continue
        unique[(pid, client.get("source", "device_handle"))] = dict(client)
    return {"state": "held" if unique else "unknown", "clients": list(unique.values()),
            "complete": False, "handle_scan_complete": bool(scan_complete),
            "scanned_handles": scanned, "reasons": list(dict.fromkeys([*_COVERAGE, *reasons]))}


def selected_input_nodes(device_id, ancestors):
    """Select one input branch, stopping before composites, hubs and radios.

The caller gets ancestors from CM_Get_Parent, never by VID/PID search. The HID
gamepad may sit below a dedicated USB/XInput function that clients open instead.
The composite controller, sibling mouse/keyboard/vendor functions, Bluetooth
radio, and USB bus are deliberately outside this scope.
    """
    if not device_id or not ancestors or ancestors[0].get("instance_id", "").casefold() != device_id.casefold():
        return ()
    if any(node.get("service", "").casefold() in _VIRTUAL_SERVICES
           or node.get("instance_id", "").upper().startswith(("ROOT\\VIGEM", "ROOT\\VJOY"))
           for node in ancestors):
        return ()
    selected = []
    for index, node in enumerate(ancestors):
        identity = node.get("instance_id", "").upper()
        service = node.get("service", "").casefold()
        if index == 0:
            if not identity.startswith("HID\\"):
                return ()
        elif not (identity.startswith("HID\\") or
                  (identity.startswith("USB\\VID_") and service in _INPUT_SERVICES) or
                  (identity.startswith(("BTHENUM\\", "BTHLEDEVICE\\", "BTHLE\\"))
                   and service in _INPUT_SERVICES)):
            break
        if node.get("pdo", "").casefold().startswith("\\device\\"):
            selected.append(dict(node))
    return tuple(selected)


def match_input_handle(record, targets, *, exclude_pids=()):
    """Match exact selected input PDOs, never VID/PID, executable or prefix."""
    if (type(record.get("pid")) is not int or record["pid"] <= 4
            or record["pid"] in exclude_pids or record.get("type") != "File"
            or not (record.get("access", 0) & 1)):
        return None
    source = targets.get(record.get("object_name", "").casefold())
    if source is None:
        return None
    # A basename is sufficient to identify a client in the UI; do not expose
    # the user's installation path or any other handle names in a report.
    return {"pid": record["pid"], "name": record.get("name", ""),
            "source": source, "evidence": "open_input_handle"}


def _decode_request(encoded):
    if not isinstance(encoded, str) or len(encoded) > 16384:
        raise ValueError("Invalid controller usage request")
    value = json.loads(base64.b64decode(encoded, validate=True))
    if (not isinstance(value, dict) or not isinstance(value.get("device_id"), str)
            or not 1 <= len(value["device_id"]) <= 1024
            or not isinstance(value.get("exclude_pids", []), list)
            or any(type(pid) is not int or pid < 0 for pid in value.get("exclude_pids", []))):
        raise ValueError("Invalid controller usage request")
    return value


def _last_report(output):
    result = None
    for line in (output or b"").decode("utf-8", "replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (isinstance(value, dict) and value.get("state") in ("held", "unknown")
                and isinstance(value.get("clients"), list)):
            result = value
    return result


def probe_controller_usage(connection, *, exclude_pids=(), timeout=6.0):
    """Return bounded, conservative input-handle evidence for one connection.

No privilege escalation is attempted. Denied processes and unavailable native
APIs make coverage incomplete rather than silently reporting no users. A caller
may refresh inputs conservatively on unknown. Do not call this on the UI thread.
    """
    if os.name != "nt":
        return _result(reasons=["windows_required"])
    if (connection.kind not in ("usb", "receiver", "bluetooth") or not connection.device_id):
        return _result(reasons=["selected_physical_input_unresolved"])
    if not 0.2 <= timeout <= 15:
        raise ValueError("Probe timeout must be between 0.2 and 15 seconds")
    request = {"device_id": connection.device_id,
               "exclude_pids": sorted({os.getpid(), *exclude_pids})}
    token = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    command = [sys.executable]
    if getattr(sys, "frozen", False):
        command += ["--controller-usage-worker", token]
    else:
        command += ["-X", "utf8", "-m", __name__, token]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, startupinfo=startup,
                                 creationflags=subprocess.CREATE_NO_WINDOW,
                                 cwd=Path(__file__).resolve().parents[3] if not getattr(sys, "frozen", False) else None)
    except OSError as exc:
        return _result(reasons=[f"probe_launch_failed:{getattr(exc, 'winerror', None)}"])
    timed_out = False
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Only the exact subprocess created above; never a game or caller PID.
        child.kill()
        output, _ = child.communicate(timeout=2)
    report = _last_report(output) or _result(reasons=["probe_no_valid_report"])
    if timed_out or child.returncode:
        report = _result(report["clients"], scanned=report.get("scanned_handles", 0),
                         reasons=[*report.get("reasons", []),
                                  "probe_timeout" if timed_out else "probe_failed"])
    return report


class _Handle(C.Structure):
    _fields_ = [("object", C.c_void_p), ("pid", C.c_size_t), ("handle", C.c_size_t),
                ("access", C.c_uint32), ("trace", C.c_uint16), ("type", C.c_uint16),
                ("attributes", C.c_uint32), ("reserved", C.c_uint32)]


class _Unicode(C.Structure):
    _fields_ = [("length", C.c_uint16), ("maximum", C.c_uint16), ("buffer", C.c_void_p)]


class _Native:
    def __init__(self):
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        self.k = C.WinDLL(str(system / "kernel32.dll"), use_last_error=True)
        self.n = C.WinDLL(str(system / "ntdll.dll"), use_last_error=True)
        self.c = C.WinDLL(str(system / "cfgmgr32.dll"), use_last_error=True)
        for library, name, args, result in (
            (self.k, "CreateFileW", [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p], C.c_void_p),
            (self.k, "CloseHandle", [C.c_void_p], C.c_int),
            (self.k, "OpenProcess", [C.c_uint32, C.c_int, C.c_uint32], C.c_void_p),
            (self.k, "GetCurrentProcess", [], C.c_void_p),
            (self.k, "DuplicateHandle", [C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_uint32, C.c_int, C.c_uint32], C.c_int),
            (self.k, "GetFileType", [C.c_void_p], C.c_uint32),
            (self.k, "QueryFullProcessImageNameW", [C.c_void_p, C.c_uint32, C.c_wchar_p, C.POINTER(C.c_uint32)], C.c_int),
            (self.n, "NtQuerySystemInformation", [C.c_uint32, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_int32),
            (self.n, "NtQueryObject", [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_int32),
            (self.c, "CM_Locate_DevNodeW", [C.POINTER(C.c_uint32), C.c_wchar_p, C.c_uint32], C.c_uint32),
            (self.c, "CM_Get_Parent", [C.POINTER(C.c_uint32), C.c_uint32, C.c_uint32], C.c_uint32),
            (self.c, "CM_Get_Device_IDW", [C.c_uint32, C.c_wchar_p, C.c_uint32, C.c_uint32], C.c_uint32),
            (self.c, "CM_Get_DevNode_Registry_PropertyW", [C.c_uint32, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p, C.POINTER(C.c_uint32), C.c_uint32], C.c_uint32),
        ):
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def nodes(self, device_id):
        node = C.c_uint32()
        if self.c.CM_Locate_DevNodeW(C.byref(node), device_id, 0):
            raise OSError("selected_input_not_present")
        result = []
        for _ in range(20):
            identifier = C.create_unicode_buffer(1024)
            if self.c.CM_Get_Device_IDW(node, identifier, len(identifier), 0):
                raise OSError("selected_input_ancestry_unavailable")
            row = {"instance_id": identifier.value}
            for label, key in (("service", 5), ("pdo", 15)):
                data = C.create_unicode_buffer(1024)
                length, kind = C.c_uint32(C.sizeof(data)), C.c_uint32()
                status = self.c.CM_Get_DevNode_Registry_PropertyW(node, key, C.byref(kind), data, C.byref(length), 0)
                row[label] = data.value if status == 0 else ""
            result.append(row)
            parent = C.c_uint32()
            if self.c.CM_Get_Parent(C.byref(parent), node, 0):
                break
            node = parent
        return selected_input_nodes(device_id, result)

    def handles(self):
        size = 4 * 1024 * 1024
        for _ in range(8):
            data, needed = C.create_string_buffer(size), C.c_uint32()
            status = self.n.NtQuerySystemInformation(64, data, size, C.byref(needed))
            if status == 0:
                count = C.c_size_t.from_buffer(data).value
                offset = C.sizeof(C.c_size_t) * 2
                if count > (size - offset) // C.sizeof(_Handle):
                    raise OSError("invalid_system_handle_table")
                return data, (_Handle * count).from_buffer(data, offset)
            if status & 0xffffffff != 0xc0000004:
                raise OSError(f"handle_enumeration_failed:{status & 0xffffffff:08x}")
            size = max(size * 2, needed.value + 65536)
            if size > 64 * 1024 * 1024:
                break
        raise OSError("handle_table_too_large")

    def text(self, handle, info_class):
        data, needed = C.create_string_buffer(65536), C.c_uint32()
        if self.n.NtQueryObject(handle, info_class, data, len(data), C.byref(needed)):
            return None
        value = _Unicode.from_buffer(data)
        start, end = C.addressof(data), C.addressof(data) + len(data)
        if not value.length:
            return ""
        if (not value.buffer or value.length % 2 or value.length > value.maximum
                or value.buffer < start or value.buffer + value.length > end):
            return None
        return C.wstring_at(value.buffer, value.length // 2)

    def process_name(self, handle):
        data, length = C.create_unicode_buffer(32768), C.c_uint32(32768)
        return Path(data.value).name if self.k.QueryFullProcessImageNameW(handle, 0, data, C.byref(length)) else ""


def _scan(request, emit):
    native = _Native()
    nodes = native.nodes(request["device_id"])
    if not nodes:
        return _result(reasons=["selected_input_pdo_unavailable"])
    targets = {node["pdo"].casefold(): "selected_hid" if index == 0 else "selected_input_parent"
               for index, node in enumerate(nodes)}
    excluded = {os.getpid(), *request.get("exclude_pids", [])}
    # NUL provides File's dynamic object-type index without opening the pad.
    marker = native.k.CreateFileW("NUL", 0, 3, None, 3, 0, None)
    if marker in (None, C.c_void_p(-1).value):
        return _result(reasons=["file_type_unavailable"])
    try:
        backing, entries = native.handles()
        file_type = next((item.type for item in entries
                          if item.pid == os.getpid() and item.handle == marker), None)
    finally:
        native.k.CloseHandle(marker)
    if file_type is None:
        return _result(reasons=["file_type_unavailable"])
    clients, processes, reasons = [], {}, []
    deadline, scanned = time.monotonic() + 4.5, 0
    try:
        for item in entries:
            if item.type != file_type or item.pid <= 4 or item.pid in excluded or not (item.access & 1):
                continue
            if time.monotonic() >= deadline:
                reasons.append("scan_deadline")
                break
            if item.pid not in processes:
                # Query-only image name and duplicate-only handle access.
                process = native.k.OpenProcess(0x1040, False, item.pid)
                processes[item.pid] = (process, native.process_name(process) if process else "")
                if not process:
                    reasons.append("process_access_denied_or_exited")
            process, name = processes[item.pid]
            if not process:
                continue
            duplicate = C.c_void_p()
            if not native.k.DuplicateHandle(process, item.handle, native.k.GetCurrentProcess(),
                                            C.byref(duplicate), 0, False, 0):
                reasons.append("handle_closed_or_inaccessible")
                continue
            try:
                scanned += 1
                # Disk, pipe and console names are unrelated and may block.
                # HID/XInput device files report FILE_TYPE_UNKNOWN (0). Any
                # unexpected driver blockage is bounded by the parent process.
                if native.k.GetFileType(duplicate) != 0:
                    continue
                if native.text(duplicate, 2) != "File":
                    reasons.append("handle_type_changed_or_unavailable")
                    continue
                object_name = native.text(duplicate, 1)
                if object_name is None:
                    reasons.append("device_handle_name_unavailable")
                    continue
                match = match_input_handle({"pid": item.pid, "access": item.access, "type": "File",
                                            "object_name": object_name, "name": name}, targets,
                                           exclude_pids=excluded)
                if match and match not in clients:
                    clients.append(match)
                    emit(_result(clients, reasons=reasons, scanned=scanned))
            finally:
                native.k.CloseHandle(duplicate)
        if native.nodes(request["device_id"]) != nodes:
            return _result(reasons=["selected_input_changed_during_scan"], scanned=scanned)
        return _result(clients, scan_complete=not reasons, reasons=reasons, scanned=scanned)
    finally:
        for process, _name in processes.values():
            if process:
                native.k.CloseHandle(process)


def worker_main(encoded):
    """CLI dispatcher; stdout is newline JSON, never private handle paths."""
    def emit(report):
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=True), flush=True)
        elif os.name == "nt":
            # Windowed PyInstaller leaves sys.stdout=None even when the parent
            # supplied an OS pipe. Write only to that inherited standard pipe.
            kernel = C.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.argtypes, kernel.GetStdHandle.restype = [C.c_uint32], C.c_void_p
            kernel.WriteFile.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p]
            kernel.WriteFile.restype = C.c_int
            handle = kernel.GetStdHandle(0xfffffff5)
            if handle not in (None, C.c_void_p(-1).value):
                data = (json.dumps(report, ensure_ascii=True) + "\n").encode("ascii")
                written = C.c_uint32()
                kernel.WriteFile(handle, data, len(data), C.byref(written), None)
    try:
        request = _decode_request(encoded)
        emit(_result(reasons=["scan_in_progress"]))
        report = _scan(request, emit)
    except Exception as exc:
        # Only our short native error codes are useful; don't print arbitrary
        # exception messages that could contain a hardware ID or a file path.
        report = _result(reasons=["native_probe_failed", type(exc).__name__])
    emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1] if len(sys.argv) == 2 else ""))
