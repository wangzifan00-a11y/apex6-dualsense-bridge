"""Read-only proof of a different full Windows kernel startup.

System event record IDs are combined with the original event contents and the
machine identity. Wall-clock time, an uptime estimate, a logon, or a service
restart alone never proves a reboot. Missing/rolled-over logs fail closed.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_GENERAL = ("Microsoft-Windows-Kernel-General", "{a68ca8b7-004f-d7b6-a698-07e2de0f1f5d}")
_BOOT = ("Microsoft-Windows-Kernel-Boot", "{15ca44ff-4d7a-4baa-bba5-0998955e531e}")


def _date(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid startup time")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Startup time must include its timezone")
    return value


def _parse_event(xml, provider, event_id):
    if not isinstance(xml, str) or len(xml) > 16384 or "<!" in xml:
        raise ValueError("Invalid startup event XML")
    root = ET.fromstring(xml)
    system = root.find(_NS + "System")
    source = system.find(_NS + "Provider")
    if (root.tag != _NS + "Event" or source.get("Name") != provider[0]
            or source.get("Guid", "").casefold() != provider[1]
            or system.findtext(_NS + "Channel") != "System"
            or system.findtext(_NS + "EventID") != str(event_id)):
        raise ValueError("Unexpected startup event source")
    record_id = int(system.findtext(_NS + "EventRecordID"))
    computer = system.findtext(_NS + "Computer")
    timestamp = system.find(_NS + "TimeCreated").get("SystemTime")
    if record_id <= 0 or not computer or len(computer) > 256:
        raise ValueError("Invalid startup event identity")
    _date(timestamp)
    data = root.find(_NS + "EventData")
    fields = {item.get("Name"): item.text for item in data}
    if len(fields) != len(data):
        raise ValueError("Duplicate startup event fields")
    event = dict(record_id=record_id, computer=computer.casefold(), time=timestamp)
    if event_id == 12:
        _date(fields.get("StartTime"))
        event["start_time"] = fields["StartTime"]
    else:
        event["boot_type"] = int(fields.get("BootType"))
    return event


def _events(provider, event_id, *, record_id=None, minimum_id=None, limit=1):
    """Query only fixed providers/IDs and numeric record IDs; never execute text."""
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    api = C.WinDLL(str(system / "wevtapi.dll"), use_last_error=True)
    api.EvtQuery.argtypes, api.EvtQuery.restype = [W.HANDLE, W.LPCWSTR, W.LPCWSTR, W.DWORD], W.HANDLE
    api.EvtNext.argtypes, api.EvtNext.restype = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE), W.DWORD, W.DWORD, C.POINTER(W.DWORD)], W.BOOL
    api.EvtRender.argtypes, api.EvtRender.restype = [W.HANDLE, W.HANDLE, W.DWORD, W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.POINTER(W.DWORD)], W.BOOL
    api.EvtClose.argtypes, api.EvtClose.restype = [W.HANDLE], W.BOOL
    query = "*[System[Provider[@Name='" + provider[0] + "'] and EventID=" + str(event_id)
    if record_id is not None:
        query += " and EventRecordID=" + str(int(record_id))
    if minimum_id is not None:
        query += " and EventRecordID>=" + str(int(minimum_id))
    query += "]]"
    handle = api.EvtQuery(None, "System", query, 0x201)  # channel path, newest first
    if not handle:
        raise C.WinError(C.get_last_error())
    result = []
    try:
        for _ in range(min(limit, 16)):
            event, count = W.HANDLE(), W.DWORD()
            if not api.EvtNext(handle, 1, C.byref(event), 1000, 0, C.byref(count)):
                if C.get_last_error() == 259:  # ERROR_NO_MORE_ITEMS
                    break
                raise C.WinError(C.get_last_error())
            try:
                size, properties = W.DWORD(), W.DWORD()
                api.EvtRender(None, event, 1, 0, None, C.byref(size), C.byref(properties))
                if C.get_last_error() != 122 or not 0 < size.value <= 32768:
                    raise ValueError("Unexpected startup event size")
                buffer = C.create_unicode_buffer((size.value + 1) // 2)
                if not api.EvtRender(None, event, 1, C.sizeof(buffer), buffer, C.byref(size), C.byref(properties)):
                    raise C.WinError(C.get_last_error())
                result.append(_parse_event(buffer.value, provider, event_id))
            finally:
                api.EvtClose(event)
    finally:
        api.EvtClose(handle)
    return result


def capture_boot_identity():
    """Return a full-startup identity or raise if reliable evidence is unavailable."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        machine, kind = winreg.QueryValueEx(key, "MachineGuid")
    if kind != winreg.REG_SZ or not isinstance(machine, str) or not machine:
        raise ValueError("Windows machine identity is unavailable")
    starts = _events(_GENERAL, 12)
    if not starts:
        raise ValueError("Windows startup event is unavailable")
    started = starts[0]
    for boot in _events(_BOOT, 27, minimum_id=started["record_id"], limit=16):
        delta = (_date(boot["time"]) - _date(started["time"])).total_seconds()
        if boot["boot_type"] == 0 and boot["computer"] == started["computer"] and 0 <= delta <= 10:
            return dict(format=1, source="windows-system-startup",
                        machine=hashlib.sha256(machine.casefold().encode()).hexdigest(),
                        started=started, full_boot=boot)
    raise ValueError("No matching full Windows startup was recorded")


def prove_later_boot(saved):
    """Return before/after evidence only when the saved kernel session ended."""
    if (not isinstance(saved, dict) or type(saved.get("format")) is not int or saved["format"] != 1
            or saved.get("source") != "windows-system-startup"
            or not isinstance(saved.get("machine"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", saved["machine"])):
        return None
    try:
        old_start, old_boot = saved["started"], saved["full_boot"]
        if (type(old_start["record_id"]) is not int or type(old_boot["record_id"]) is not int
                or type(old_boot["boot_type"]) is not int or old_boot["boot_type"] != 0):
            return None
        current = capture_boot_identity()
        if (current["machine"] != saved["machine"]
                or current["started"]["record_id"] <= old_boot["record_id"]
                or _date(current["started"]["start_time"]) <= _date(old_start["start_time"])):
            return None
        # A reused ID after log clearing cannot match the original event pair.
        if (_events(_GENERAL, 12, record_id=old_start["record_id"]) != [old_start]
                or _events(_BOOT, 27, record_id=old_boot["record_id"]) != [old_boot]):
            return None
        return dict(boot_before=saved, boot_after=current)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
