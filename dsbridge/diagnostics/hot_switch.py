"""Opt-in zero-gain hot-switch audit with a pre-existing unallowed consumer.

The observer never sends controller output. One original HID handle survives
across the switch; fresh handles after stop are separately labelled. A passed
diagnostic proves these Windows input observations, not compatibility with an
unobserved game. Only the normal start/stop case is implemented here.
"""
from __future__ import annotations

import ctypes as C
from copy import deepcopy
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import time
import uuid

from dsbridge.runtime.paths import as_paths

_TOKEN = re.compile(r"[0-9a-f]{32}")
_PENDING = []
# Invalid handle / no such device / device not connected. A generic I/O failure
# (31 or 1117), cancellation (995), and incomplete I/O (996) are not this proof.
_REMOVED = {6, 433, 1167}
_JSON_IO_ATTEMPTS = 40
_JSON_IO_DELAY = .02


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _retry_json_io(operation):
    """Bound Windows replace/open conflicts without hiding permanent failures.

    The CRT can expose the same conflict as errno EACCES without winerror.
    Access denied is therefore retried briefly too, but the final error still
    propagates. No retry covers JSON decoding or a partly completed write.
    """
    for attempt in range(_JSON_IO_ATTEMPTS):
        try:
            return operation()
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            transient = winerror in (5, 32, 33) or (winerror is None and exc.errno == errno.EACCES)
            if not transient or attempt + 1 == _JSON_IO_ATTEMPTS:
                raise
            time.sleep(_JSON_IO_DELAY)


def read(path):
    # Parse only a completed read. Invalid/truncated JSON is never accepted or
    # silently replaced by an older observation.
    return json.loads(_retry_json_io(lambda: Path(path).read_text(encoding="utf-8")))


def save_json(path, value):
    """Publish a complete diagnostic message with a retryable atomic replace.

    Each IPC path has one writer. Readers only open the published .json file;
    while a reader holds it, Windows may temporarily refuse its replacement.
    Keep the fully flushed temporary file intact while retrying that operation.
    """
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(".tmp")
    with _retry_json_io(lambda: temporary.open("w", encoding="utf-8")) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _retry_json_io(lambda: temporary.replace(path))


def _files(paths, token):
    require(isinstance(token, str) and _TOKEN.fullmatch(token), "无效的热切换观察标识")
    root = as_paths(paths).state
    return tuple(root / f"hot-switch-observer-{kind}-{token}.json" for kind in ("request", "control", "report"))


class Evidence:
    """Pure assertions about one old reader and separately opened new readers."""
    def __init__(self):
        self._bridge_since = self._restored_since = None
        self._bridge_samples = self._restored_samples = 0
        self.value = {"baseline": False, "old_hid_open_count": 1, "old_hid_invalidated": False,
                      "old_hid_error": None, "physical_xinput_absent": False, "virtual_sony_arrived": False,
                      "physical_xinput_restored": False, "fresh_hid_opened_after_stop": False,
                      "virtual_sony_removed": False, "old_hid_reports": 0, "fresh_hid_reports": 0,
                      "hidden_stable": False, "restored_stable": False}

    def observe(self, phase, *, physical, sony_count, old_read_started, old_reports, old_error,
                fresh_open=False, fresh_reports=0, at=None):
        at = time.monotonic() if at is None else at
        value = self.value
        require(type(sony_count) is int and 0 <= sony_count <= 1,
                "观察到多个 DS 输入或无法确认 DS 数量，审计已停止")
        value["old_hid_reports"] = old_reports
        value["fresh_hid_reports"] = fresh_reports
        if phase == "baseline":
            require(not old_error, "实体 HID 旧连接在桥接前已失效")
            require(sony_count == 0, "测试开始前已有虚拟 DS，不能归属于本次桥接")
            value["baseline"] = bool(physical and old_read_started)
        elif phase == "bridge":
            require(value["baseline"], "缺少桥接前的旧输入连接证据")
            if old_error in _REMOVED:
                value["old_hid_invalidated"] = True
                value["old_hid_error"] = old_error
            value["physical_xinput_absent"] |= not physical
            value["virtual_sony_arrived"] |= sony_count == 1
            if value["old_hid_invalidated"] and not physical and sony_count == 1:
                if self._bridge_since is None:
                    self._bridge_since = at
                self._bridge_samples += 1
                value["hidden_stable"] |= at - self._bridge_since >= .75 and self._bridge_samples >= 3
            else:
                self._bridge_since, self._bridge_samples = None, 0
        elif phase == "restored":
            value["physical_xinput_restored"] |= physical
            value["fresh_hid_opened_after_stop"] |= bool(fresh_open)
            value["virtual_sony_removed"] |= sony_count == 0
            if physical and fresh_open and fresh_reports > 0 and sony_count == 0:
                if self._restored_since is None:
                    self._restored_since = at
                self._restored_samples += 1
                value["restored_stable"] |= at - self._restored_since >= .75 and self._restored_samples >= 3
            else:
                self._restored_since, self._restored_samples = None, 0

    def switched(self):
        return all(self.value[key] for key in ("baseline", "old_hid_invalidated", "physical_xinput_absent", "virtual_sony_arrived", "hidden_stable"))

    def restored(self):
        return self.switched() and all(self.value[key] for key in
            ("physical_xinput_restored", "fresh_hid_opened_after_stop", "virtual_sony_removed", "restored_stable"))


class _Reader:
    """One read-only physical HID handle, never reopened by this object."""
    def __init__(self, path, vendor, product):
        from dsbridge.diagnostics.sony_writer import _Api
        from dsbridge.diagnostics.touchpad import _Overlapped
        self.api, self.overlapped_type = _Api(), _Overlapped
        self.handle = self.event = None
        self.pending = False
        self.started = self.reports = 0
        self.error = None
        k = self.api.kernel
        for name, args, result in (
            ("ReadFile", [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.POINTER(_Overlapped)], C.c_int),
            ("CreateEventW", [C.c_void_p, C.c_int, C.c_int, C.c_wchar_p], C.c_void_p),
            ("ResetEvent", [C.c_void_p], C.c_int),
            ("WaitForSingleObject", [C.c_void_p, C.c_uint32], C.c_uint32),
            ("CancelIoEx", [C.c_void_p, C.POINTER(_Overlapped)], C.c_int),
            ("GetOverlappedResult", [C.c_void_p, C.POINTER(_Overlapped), C.POINTER(C.c_uint32), C.c_int], C.c_int),
        ):
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result
        try:
            self.handle = k.CreateFileW(path, 0x80000000, 3, None, 3, 0x40000000, None)
            require(self.handle not in (None, C.c_void_p(-1).value), "观察进程无法只读打开实体 HID")
            attrs, caps = self.api.inspect(self.handle)
            require((attrs.VendorID, attrs.ProductID) == (vendor, product)
                    and caps.UsagePage == 1 and caps.Usage in (4, 5)
                    and 1 <= caps.InputReportByteLength <= 512, "观察到的实体 HID 身份或输入格式不匹配")
            self.buffer = C.create_string_buffer(caps.InputReportByteLength)
            self.overlapped = _Overlapped()
            self.event = k.CreateEventW(None, True, False, None)
            require(self.event, "无法建立实体 HID 读取事件")
        except Exception:
            self.close()
            raise

    @property
    def read_active(self):
        return not self.error and bool(self.pending or self.reports)

    def _done(self, *, cleanup=False):
        count = C.c_uint32()
        ok = self.api.kernel.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(count), False)
        error = 0 if ok else C.get_last_error()
        if error == 996:  # ERROR_IO_INCOMPLETE: the buffer is still owned by I/O.
            return False
        self.pending = False
        if cleanup:
            return True
        if error in _REMOVED:
            self.error = error
        elif error == 995:
            pass  # A cancelled read alone does not prove this old handle died.
        else:
            require(ok and count.value == len(self.buffer), "实体 HID 读取未完整结束：" + str(error))
            self.reports += 1
        return True

    def poll(self):
        if self.error:
            return
        k = self.api.kernel
        if self.pending:
            wait = k.WaitForSingleObject(self.event, 0)
            if wait == 258:
                return
            require(wait == 0, "实体 HID 读取等待失败")
            self._done()
            return
        require(k.ResetEvent(self.event), "无法复位实体 HID 读取事件")
        self.overlapped = self.overlapped_type()
        self.overlapped.hEvent = self.event
        self.pending = True
        self.started += 1
        if k.ReadFile(self.handle, self.buffer, len(self.buffer), None, C.byref(self.overlapped)):
            self._done()
            return
        error = C.get_last_error()
        if error != 997:
            self.pending = False
            if error in _REMOVED:
                self.error = error
            elif error != 995:
                raise RuntimeError("实体 HID 读取请求失败：" + str(error))

    def close(self):
        k = self.api.kernel
        if self.pending:
            k.CancelIoEx(self.handle, C.byref(self.overlapped))
            if k.WaitForSingleObject(self.event, 2000) == 0:
                self._done(cleanup=True)
        if self.handle not in (None, C.c_void_p(-1).value):
            k.CloseHandle(self.handle)
            self.handle = None
        if self.pending and k.WaitForSingleObject(self.event, 2000) == 0:
            self.pending = False
        if self.pending:
            _PENDING.append(self)  # Keep I/O memory alive until the direct process exit below.
            raise RuntimeError("观察进程仍有未结束的 HID 读取")
        if self.event:
            k.CloseHandle(self.event)
            self.event = None


def _physical_path(connection):
    from dsbridge.platform.windows.connection import present_gamepads
    rows = present_gamepads(connection.vendor_id, connection.product_id)
    require(len(rows) == 1 and rows[0]["instance_id"].casefold() == connection.device_id.casefold(),
            "无法唯一对应所选实体手柄的 HID 输入")
    return rows[0]["path"]


def _current_connection(connection, targets, anchor):
    """Resolve a new leaf only beneath the pre-switch physical anchor."""
    from dsbridge.platform.windows.input_transition_targets import resolve_current_input
    current = resolve_current_input(targets, anchor)
    require(current.restart_id.casefold() == targets.restart_id.casefold()
            and current.transport == targets.transport, "恢复后的输入不属于原物理手柄")
    return replace(connection, device_id=current.device_id)


def _verify_hidden_input(connection, targets, anchor):
    """A separate new open must be denied; the old reader stays untouched."""
    from dsbridge.diagnostics.sony_writer import _Api, INVALID_HANDLE_VALUE
    current = _current_connection(connection, targets, anchor)
    path = _physical_path(current)
    api = _Api()
    handle = api.kernel.CreateFileW(path, 0x80000000, 3, None, 3, 0x40000000, None)
    if handle not in (None, INVALID_HANDLE_VALUE):
        api.kernel.CloseHandle(handle)
        raise RuntimeError("独立观察程序仍能打开当前实体 HID，新输入尚未隔离")
    error = C.get_last_error()
    require(error == 5, f"当前实体 HID 打开失败，但不是明确拒绝访问（{error}），不能证明隔离")
    again = _current_connection(connection, targets, anchor)
    require(again.device_id.casefold() == current.device_id.casefold()
            and _physical_path(again).casefold() == path.casefold(),
            "拒绝访问检查期间实体输入已变化，不能证明隔离")
    return {"device_id": current.device_id, "blocked": True, "win32_error": error,
            "physical_anchor_verified": True, "no_output": True}


def _physical_connected(xinput, connection):
    count = 0
    for slot in range(4):
        identity = xinput.identity(slot)
        if identity is None:
            continue  # XInputGetState explicitly returned ERROR_DEVICE_NOT_CONNECTED.
        # GetState can succeed while the extended identity query fails during
        # a device transition. Such a live slot is unknown, never proof that
        # the selected physical controller disappeared from this consumer.
        require(all(type(identity.get(key)) is int and 0 < identity[key] <= 0xFFFF
                    for key in ("vendor_id", "product_id")),
                f"XInput 槽位 {slot} 仍在线但身份无法确认，不能判定实体输入已断开")
        if (identity["vendor_id"], identity["product_id"]) == (connection.vendor_id, connection.product_id):
            count += 1
    require(count <= 1, "观察到多个同型号实体输入，测试无法唯一归属")
    return count == 1


def _present_dualsense_count():
    """Count present DS gamepad interfaces; incomplete inspection is unknown.

    The general writer's enumeration intentionally skips unreadable devices.
    That is suitable for choosing a writable target, but cannot prove absence
    or uniqueness in this audit. Here every detail query must complete and
    every Sony candidate must be inspected without sending an output report.
    """
    from dsbridge.diagnostics.sony_writer import (
        _Api, DWORD, GUID, SP_DEVICE_INTERFACE_DATA, SP_DEVINFO_DATA,
        INVALID_HANDLE_VALUE, _matching,
    )
    api = _Api()
    guid = GUID()
    api.hid.HidD_GetHidGuid(C.byref(guid))
    info = api.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x02 | 0x10)
    require(info is not None and info != INVALID_HANDLE_VALUE, "无法完整枚举当前 HID 输入")
    count = 0
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = C.sizeof(interface)
            if not api.setup.SetupDiEnumDeviceInterfaces(info, None, C.byref(guid), index, C.byref(interface)):
                require(C.get_last_error() == 259, "当前 HID 输入枚举未完整结束")
                return count
            index += 1
            required = DWORD()
            api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), None, 0,
                                                      C.byref(required), None)
            require(8 <= required.value <= 65536, "无法读取当前 HID 输入路径，不能确认 DS 数量")
            detail = C.create_string_buffer(required.value)
            DWORD.from_buffer(detail).value = 8 if C.sizeof(C.c_void_p) == 8 else 6
            devinfo = SP_DEVINFO_DATA()
            devinfo.cbSize = C.sizeof(devinfo)
            require(api.setup.SetupDiGetDeviceInterfaceDetailW(
                info, C.byref(interface), detail, required, C.byref(required), C.byref(devinfo)),
                "当前 HID 输入路径查询失败，不能确认 DS 数量")
            path = C.wstring_at(C.addressof(detail) + 4)
            if "vid_054c&pid_0ce6" not in path.casefold():
                continue
            handle = api.open(path)
            try:
                attrs, caps = api.inspect(handle)
            finally:
                api.kernel.CloseHandle(handle)
            require(attrs.VendorID == 0x054C and attrs.ProductID == 0x0CE6,
                    "DS 输入路径与设备属性不一致，不能确认数量")
            if caps.UsagePage != 1 or caps.Usage != 5:
                continue  # A fully inspected non-gamepad HID collection.
            require(_matching(attrs, caps) and api.usbip_ancestry(devinfo.DevInst),
                    "存在无法确认属于本次虚拟手柄类型的 DS 输入")
            count += 1
    finally:
        api.setup.SetupDiDestroyDeviceInfoList(info)


def _write_observer_final(path, report):
    try:
        save_json(path, report)
    finally:
        if _PENDING:
            # Avoid Python finalizers freeing an outstanding OVERLAPPED or
            # buffer. Only this read-only worker exits; Windows owns teardown.
            os._exit(1)


def observer_main(base, token):
    from dsbridge.core.connection import ControllerConnection
    from dsbridge.diagnostics.hidhide import read_config
    from dsbridge.platform.windows.xinput import XInput
    from dsbridge.platform.windows.input_transition_targets import (
        capture_input_anchor, resolve_input_targets,
    )
    from dsbridge.controllers.flydigi.apex6.recovery import Native
    paths = as_paths(base)
    request_file, control_file, report_file = _files(paths, token)
    report = {"format": 1, "token": token, "pid": os.getpid(), "phase": "starting",
              "passed": False, "no_output": True, "game_validation": False, "cleanup_complete": False}
    save_json(report_file, report)
    evidence, old, fresh, xi, parent = Evidence(), None, None, None, None
    native = Native()
    try:
        request = read(request_file)
        require(request.get("token") == token and request.get("format") == 1, "观察请求不匹配")
        require(type(request.get("parent_pid")) is int and request["parent_pid"] > 0, "观察父进程无效")
        connection = ControllerConnection(**request["connection"])
        require(getattr(sys, "frozen", False), "旧句柄观察必须使用打包版独立输入检查程序")
        config = read_config()
        require(not config["inverse"] and str(Path(sys.executable).resolve()).casefold() not in
                {str(Path(p).resolve()).casefold() for p in config["allowed_apps"]}, "观察程序必须处于 HidHide 允许列表之外")
        parent = native.k.OpenProcess(0x100000, False, request["parent_pid"])
        require(parent, "无法核对观察父进程")
        targets = resolve_input_targets(connection)
        anchor = capture_input_anchor(targets)
        report.update(original_input_id=connection.device_id, restart_id=targets.restart_id)
        xi = XInput()
        old = _Reader(_physical_path(connection), connection.vendor_id, connection.product_id)
        deadline, last_save = time.monotonic() + 360, 0.0
        while time.monotonic() < deadline:
            if native.k.WaitForSingleObject(parent, 0) == 0:
                raise RuntimeError("测试父进程已退出，观察已结束")
            control = read(control_file)
            require(control.get("token") == token, "观察控制记录不属于当前测试")
            phase = control.get("phase")
            require(phase in ("baseline", "bridge", "verify_hidden", "restored", "stop"), "观察阶段无效")
            if phase == "stop":
                report["passed"] = evidence.restored()
                break
            old.poll()
            physical = _physical_connected(xi, connection)
            sony_count = _present_dualsense_count()
            if phase == "verify_hidden" and "hidden_input" not in report:
                report["hidden_input"] = _verify_hidden_input(connection, targets, anchor)
            if phase == "restored" and physical:
                if fresh is None:
                    current = _current_connection(connection, targets, anchor)
                    fresh = _Reader(_physical_path(current), connection.vendor_id, connection.product_id)
                    report.update(fresh_input_id=current.device_id, fresh_physical_anchor_verified=True)
                fresh.poll()
            evidence.observe("bridge" if phase == "verify_hidden" else phase, physical=physical, sony_count=sony_count,
                             old_read_started=old.read_active, old_reports=old.reports, old_error=old.error,
                             fresh_open=bool(fresh and fresh.read_active),
                             fresh_reports=fresh.reports if fresh else 0)
            report.update(phase=phase, evidence=dict(evidence.value), switched=evidence.switched(),
                          restored=evidence.restored(), physical_currently_connected=physical,
                           sony_current_count=sony_count, sony_enumeration_complete=True)
            if time.monotonic() - last_save >= .1:
                save_json(report_file, report)
                last_save = time.monotonic()
            time.sleep(.01)
        else:
            raise RuntimeError("旧句柄观察超时")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        errors = []
        for resource in (fresh, old, xi):
            if resource:
                try:
                    resource.close()
                except Exception as exc:
                    errors.append(str(exc))
        native.close(parent)
        report.update(phase="finished", cleanup_complete=not errors, cleanup_errors=errors,
                      evidence=dict(evidence.value))
        report["passed"] = report["passed"] and not errors and not report.get("error")
        _write_observer_final(report_file, report)
    return 0 if report["passed"] else 1


def motor_snapshot(connection):
    """Read the APEX preset and volatile motor mapping without setting modes."""
    if (connection.vendor_id, connection.product_id) != (0x37D7, 0x2502):
        return {"applicable": False, "reason": "this_transport_has_no_vendor_motor_configuration"}
    from dsbridge.controllers.flydigi.apex6.hid import ReceiverHID, USAGE_PAGE, receiver_interfaces
    from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol
    from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim
    claim = ReceiverClaim()
    try:
        rows = [row for row in receiver_interfaces() if row.get("usage_page") == USAGE_PAGE
                and row.get("input_length") == row.get("output_length") == 33]
        require(len(rows) == 1, "马达配置快照需要唯一的八爪鱼控制接口")
        with ReceiverHID(rows[0]["path"]) as transport:
            protocol = ReceiverProtocol(transport)
            identity = protocol.identity()
            slot, mapping = protocol.motor_mapping()
            return {"applicable": True, "identity_sha256": hashlib.sha256(identity["uid"].encode()).hexdigest(),
                    "slot": slot, "mapping_hex": mapping.hex()}
    finally:
        claim.close()


def _audio_defaults():
    from dsbridge.platform.windows.audio import WindowsAudio
    with WindowsAudio() as audio:
        return audio.snapshot()["defaults"]


def _running_audio_snapshot(backend):
    # Read on both sides of the independent Windows snapshot so a stopped or
    # failed protection thread cannot satisfy this running-session observation.
    before = backend.status().get("audio_protection")
    defaults = _audio_defaults()
    after = backend.status().get("audio_protection")
    return {"defaults": defaults, "protection_before": before, "protection_after": after}


def _running_audio_matches(report):
    during = report.get("during_audio") or {}
    before = report.get("before") or {}
    return ("audio_defaults" in before and "defaults" in during
            and during["defaults"] == before["audio_defaults"]
            and all(isinstance(during.get(key), dict) and during[key].get("active") is True
                    and not during[key].get("error") for key in ("protection_before", "protection_after")))


def _running_isolation_snapshot(transition):
    """Capture ready-time proof before stop migrates the physical HID again."""
    require(transition.status().get("phase") == "ready" and not transition.error,
            "输入切换尚未就绪，不能确认隔离证据")
    lease = transition._lease  # Diagnostic-only access; normal UI omits device IDs.
    state = lease.status()
    require(state.get("phase") == "ready" and not state.get("error"), "输入隔离守护进程尚未就绪")
    result = deepcopy({key: state.get(key) for key in
        ("phase", "hidden", "current_targets", "input_anchor", "restart_result", "isolation")})
    result["verified_current_input_id"] = lease.current_connection.device_id
    return result


def _isolation_matches(report):
    """Independently check final IDs were hidden before the last refresh.

    Denying a new open after refresh alone cannot invalidate a connection
    acquired in the interval before a newly enumerated HID was hidden.
    """
    def ids(value):
        if not isinstance(value, list) or not value:
            raise ValueError("missing input IDs")
        if any(not isinstance(item, str) or not item or "\0" in item for item in value):
            raise ValueError("invalid input ID")
        result = {item.casefold() for item in value}
        if len(result) != len(value):
            raise ValueError("duplicate input IDs")
        return result

    try:
        during = report["during_isolation"]
        proof, current = during["isolation"], during["current_targets"]
        restart, anchor = during["restart_result"], during["input_anchor"]
        rounds = proof["rounds"]
        if (during["phase"] != "ready" or during["hidden"] is not True
                or proof["converged"] is not True or type(rounds) is not int or rounds not in (1, 2)
                or restart["ok"] is not True or restart["restart_required"] is not False):
            return False
        final = ids(proof["final_hidden_ids"])
        before = ids(proof["hidden_before_last_refresh"])
        if not final <= before or final != ids(current["hidden_ids"]):
            return False
        device_id = current["device_id"].casefold()
        root_id = current["restart_id"].casefold()
        hidden = report["observer"]["hidden_input"]
        if (device_id not in final or root_id not in final
                or any(value.casefold() != device_id for value in
                    (proof["final_input_id"], during["verified_current_input_id"], hidden["device_id"]))
                or any(value.casefold() != root_id for value in
                    (anchor["root_id"], restart["root_id"], report["baseline"]["restart_id"]))
                or current["transport"] != report["transport"]):
            return False
        if current["transport"] in ("usb", "receiver"):
            if (restart["method"] != "usb_hub_cycle_port" or restart["cycle_attempted"] is not True
                    or restart["new_input_id"].casefold() != device_id
                    or restart["physical_root_id"].casefold() != anchor["physical_root_id"].casefold()
                    or restart["hub_id"].casefold() != anchor["hub_id"].casefold()):
                return False
        elif current["transport"] != "bluetooth" or restart["method"] != "cm_disable_enable":
            return False
        attempts = proof["evidence"]
        if not isinstance(attempts, list) or len(attempts) != rounds:
            return False
        for number, attempt in enumerate(attempts, 1):
            covered = ids(attempt["current"]) <= ids(attempt["pre_hidden"])
            if (type(attempt["round"]) is not int or attempt["round"] != number
                    or attempt["active"] is not True or attempt["inverse"] is not False
                    or attempt["native_success"] is not True or attempt["verified"] is not covered
                    or attempt["input_id"].casefold() not in ids(attempt["current"])
                    or (number < rounds and covered)):
                return False
        last = attempts[-1]
        return (last["verified"] is True and ids(last["current"]) == final
                and ids(last["pre_hidden"]) == before and last["input_id"].casefold() == device_id)
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def system_snapshot():
    from dsbridge.diagnostics.hidhide import read_config
    from dsbridge.controllers.flydigi.apex6.recovery import RUN_KEY, RUN_NAME
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            logon = winreg.QueryValueEx(key, RUN_NAME)[0]
    except FileNotFoundError:
        logon = None
    return {"hidhide": read_config(), "audio_defaults": _audio_defaults(), "temporary_logon_entry": logon}


class _ZeroInput:
    def __init__(self, source):
        self.source, self.nonzero_attempts = source, 0

    def __getattr__(self, name):
        return getattr(self.source, name)

    def set_rumble(self, slot, left, right):
        if left or right:
            self.nonzero_attempts += 1
            raise RuntimeError("热切换零强度测试拒绝了非零震动")
        return self.source.set_rumble(slot, 0, 0)


def audit_passed(report):
    observer = report.get("observer", {})
    evidence = observer.get("evidence", {})
    session = report.get("session_end") or {}
    hidden = observer.get("hidden_input") or {}
    return bool(not report.get("error") and not report.get("cleanup_errors")
                and report.get("zero_gain") is True and report.get("game_validation") is False
                and report.get("engine_stopped") is True and report.get("observer_exited") is True
                and observer.get("passed") is True and observer.get("cleanup_complete") is True
                and observer.get("no_output") is True and not observer.get("error")
                and observer.get("sony_enumeration_complete") is True
                and observer.get("fresh_physical_anchor_verified") is True
                and hidden.get("blocked") is True and hidden.get("win32_error") == 5
                and hidden.get("physical_anchor_verified") is True and hidden.get("no_output") is True
                and all(evidence.get(key) is True for key in ("baseline", "old_hid_invalidated", "physical_xinput_absent",
                    "virtual_sony_arrived", "physical_xinput_restored", "fresh_hid_opened_after_stop", "virtual_sony_removed",
                    "hidden_stable", "restored_stable"))
                and evidence.get("old_hid_open_count") == 1
                and evidence.get("old_hid_error") in _REMOVED
                and type(evidence.get("fresh_hid_reports")) is int and evidence["fresh_hid_reports"] > 0
                and "before" in report and report.get("before") == report.get("after")
                and _running_audio_matches(report)
                and _isolation_matches(report)
                and "motor_before" in report and report.get("motor_before") == report.get("motor_after")
                and report.get("nonzero_input_attempts") == 0 and session.get("output_nonzero_writes") == 0
                and session.get("phase") == "stopped" and not session.get("error") and session.get("gain") == 0
                and len(session.get("output_peak", ())) == 2 and not any(session["output_peak"])
                and report.get("motor_nonzero_writes") == 0)


def run_audit(context):
    """Normal case only; called by the main allowed frozen executable."""
    from dsbridge.core.engine import Engine
    from dsbridge.platform.windows.hot_switch_service import HotSwitchClient
    paths, events = context.paths, queue.Queue()
    output = paths.state / "hot-switch-audit.json"
    report = {"format": 1, "case": "normal", "phase": "starting", "passed": False,
              "pid": os.getpid(), "game_validation": False, "zero_gain": True,
              "synthetic_input": False, "synthetic_feedback": False}
    save_json(output, report)
    engine = observer = source = connection = None
    observer_files = None
    backends, transitions = [], []
    errors, cleanup = [], []
    token = uuid.uuid4().hex
    started = False

    def drain():
        nonlocal started
        while True:
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == "status" and value.startswith("已启动"):
                started = True
            elif kind in ("error", "disconnected"):
                errors.append(str(value))
            elif kind == "session_end":
                report["session_end"] = value

    def await_observer(predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            drain()
            require(not errors, "；".join(errors))
            if observer_files[2].is_file():
                value = read(observer_files[2])
                require(not value.get("error"), value.get("error", ""))
                if predicate(value):
                    return value
            require(observer.poll() is None, "旧句柄观察进程提前退出")
            time.sleep(.05)
        raise RuntimeError("等待旧句柄观察证据超时")

    try:
        require(getattr(sys, "frozen", False), "真实旧句柄审计必须使用完整打包版")
        helper = paths.assets / "接收器输入检查.exe"
        require(helper.is_file() and hashlib.sha256(helper.read_bytes()).digest() == hashlib.sha256(Path(sys.executable).read_bytes()).digest(),
                "缺少同一构建的独立输入观察程序")
        HotSwitchClient(paths).status()  # Audit never prompts for installation.
        report["before"] = system_snapshot()
        config = report["before"]["hidhide"]
        allowed = {str(Path(path).resolve()).casefold() for path in config["allowed_apps"]}
        require(str(Path(sys.executable).resolve()).casefold() in allowed, "请先允许此构建读取 HidHide 隐藏输入")
        require(str(helper.resolve()).casefold() not in allowed and not config["inverse"], "观察程序必须保持在 HidHide 允许列表之外")
        require(report["before"]["temporary_logon_entry"] is None, "存在待完成的输入恢复，未开始审计")
        source = _ZeroInput(context.create_input(1))
        slots = [slot for slot in source.slots if source.get_state(slot) is not None]
        require(len(slots) == 1, "热切换审计需要唯一且已唤醒的实体手柄")
        connection = source.connection(slots[0])
        mode = context.connection_profile(1, connection)
        source.close()
        source = _ZeroInput(context.create_input(mode))
        report["transport"] = connection.kind
        report["motor_before"] = motor_snapshot(connection)
        observer_files = _files(paths, token)
        request = {"format": 1, "token": token, "parent_pid": os.getpid(),
                   "connection": {key: value for key, value in connection.to_dict().items() if key != "label"}}
        save_json(observer_files[0], request)
        save_json(observer_files[1], {"token": token, "phase": "baseline"})
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        observer = subprocess.Popen(paths.command("--hot-switch-observer", token, executable=helper),
                                    startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
        report["observer_pid"] = observer.pid
        report["baseline"] = await_observer(lambda value: value.get("evidence", {}).get("baseline"), 15)

        def make_backend(make):
            backend = make()
            if hasattr(backend, "set_gain"):
                backend.set_gain(0)
            backends.append(backend)
            return backend

        def transition_factory(port, index, profile, stream):
            transition = context.create_transition(port, index, profile, stream)
            require(transition is not None, "生产输入切换未启用")
            original = transition.create_backend
            transition.create_backend = lambda: make_backend(original)
            transitions.append(transition)
            return transition

        engine = Engine(source, events, lambda profile: make_backend(lambda: context.create_backend(profile)),
                        profiles=context.registry.profiles, diagnostics=context.diagnostics,
                        transition_factory=transition_factory)
        engine.gain = 0
        save_json(observer_files[1], {"token": token, "phase": "bridge"})
        await_observer(lambda value: value.get("phase") == "bridge", 5)
        engine.start(slots[0], mode)
        report["phase"] = "switching"
        save_json(output, report)
        report["during"] = await_observer(lambda value: started and value.get("switched")
                                          and not value.get("physical_currently_connected") and value.get("sony_current_count") == 1, 120)
        require(engine.running, "桥接在旧句柄验证期间提前停止")
        save_json(observer_files[1], {"token": token, "phase": "verify_hidden"})
        report["isolated_observation"] = await_observer(lambda value: value.get("phase") == "verify_hidden"
            and value.get("hidden_input", {}).get("blocked") is True
            and not value.get("physical_currently_connected") and value.get("sony_current_count") == 1, 10)
        require(engine.running, "桥接在新实体输入隔离验证期间提前停止")
        require(transitions, "缺少本次输入切换的隔离证据")
        report["during_isolation"] = _running_isolation_snapshot(transitions[-1])
        require(_isolation_matches({**report, "observer": report["isolated_observation"]}),
                "最终输入未能证明在最后一次刷新前已隐藏，旧连接隔离审计未通过")
        require(backends, "缺少正在运行的桥接后端")
        report["during_audio"] = _running_audio_snapshot(backends[-1])
        require(_running_audio_matches(report), "桥接运行期间默认音频已改变或音频保护未正常运行")
        require(engine.running, "桥接在运行中音频验证期间提前停止")
        engine.stop()
        engine.thread.join(75)
        require(not engine.running, "生产桥接仍在恢复输入，保留审计记录")
        drain()
        save_json(observer_files[1], {"token": token, "phase": "restored"})
        report["restored_observation"] = await_observer(lambda value: value.get("restored")
            and value.get("physical_currently_connected") and value.get("sony_current_count") == 0, 20)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if engine:
            engine.stop()
            if engine.thread:
                engine.thread.join(75)
            drain()
        report["engine_stopped"] = not engine or not engine.running
        if observer and observer_files:
            save_json(observer_files[1], {"token": token, "phase": "stop"})
            try:
                observer.wait(timeout=12)
            except subprocess.TimeoutExpired:
                cleanup.append("只读观察进程未及时退出，保留进程以便检查")
            report["observer_exited"] = observer.poll() is not None
            if observer_files[2].is_file():
                report["observer"] = read(observer_files[2])
        if source:
            report["nonzero_input_attempts"] = source.nonzero_attempts
            try:
                source.close()
            except Exception as exc:
                cleanup.append(str(exc))
        if report["engine_stopped"]:
            try:
                report["after"] = system_snapshot()
                if connection:
                    report["motor_after"] = motor_snapshot(connection)
            except Exception as exc:
                cleanup.append(str(exc))
        else:
            cleanup.append("桥接引擎未退出，未读取可能仍被占用的马达配置")
        if backends:
            try:
                status = backends[-1].status()
                if report.get("motor_before", {}).get("applicable"):
                    writes = status.get("receiver", {}).get("motor_nonzero_writes")
                    require(isinstance(writes, (tuple, list)) and len(writes) == 4,
                            "缺少接收器四马达输出审计记录")
                    report["motor_nonzero_writes"] = sum(writes)
                else:
                    report["motor_nonzero_writes"] = 0
            except Exception as exc:
                cleanup.append(str(exc))
        else:
            report["motor_nonzero_writes"] = 0
        report["cleanup_errors"] = cleanup + errors
        report["phase"] = "finished"
        report["passed"] = audit_passed(report)
        save_json(output, report)
    require(report["passed"], report.get("error") or "热切换旧句柄审计未通过，请查看保留的审计记录")
    return report
