"""Bounded Windows client for the narrowly scoped DSBridgeHotSwitch service.

All synchronous pipe I/O lives in an owned worker process. Cancelling or timing
out stops waiting and terminates that worker, never a game or the service. An
already accepted restart may still finish in the service; callers must retain
their ordinary input-recovery path when cancellation interrupts startup.
"""
from __future__ import annotations

import base64
import ctypes as C
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from dsbridge.runtime.paths import as_paths

SERVICE_NAME = "DSBridgeHotSwitch"
PIPE_NAME = r"\\.\pipe\DSBridgeHotSwitch.v1"
API_VERSION = 1
HELPER_VERSION = "1.0.6"
_DEVICE = re.compile(r"HID\\[^\\\x00-\x20\x7f]{1,240}\\[^\\\x00-\x20\x7f]{1,240}", re.I)
_SID = re.compile(r"S-1-(?:\d+-)*\d+", re.I)
_MAX_REPLY = 16384
_DETAIL_INTS = ("disable_result", "enable_result", "status", "problem", "status_before", "problem_before",
                "flags_before", "flags_after", "flags_added", "api_version", "port_index",
                "cycle_win32_error", "cycle_usb_status", "cycle_bytes", "input_status", "input_problem")
_DETAIL_BOOLS = ("restart_required", "pre_needs_restart", "recovery_pending", "recovery_restart_required",
                 "cycle_attempted", "enable_attempted")
_ERROR_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NATIVE_MESSAGES = {
    "recovery_pending": "手柄输入仍待恢复，请查看诊断记录后重试。",
    "restart_required": "Windows 要求重启，尚未确认手柄输入已恢复。",
    "disable_failed": "Windows 未能停用所选手柄输入，请查看诊断记录。",
    "usb_cycle_failed": "Windows 未能完成所选手柄的 USB 重连，请查看诊断记录。",
    "usb_reenumeration_pending": "正在等待原手柄重新连接，恢复记录已保留。",
    "usb_identity_changed": "所选 USB 端口的设备身份已改变，请重新检测手柄。",
    "usb_port_ambiguous": "无法唯一确认手柄所在的 USB 端口，自动切换未执行。",
    "usb_query_failed": "无法核对手柄的 USB 连接信息，自动切换未执行。",
    "usb_serial_unavailable": "无法核对手柄的 USB 序列号，自动切换未执行。",
    "device_absent": "未找到所选手柄，请重新连接后重试。",
    "device_not_started": "所选手柄输入尚未就绪，请重新连接后重试。",
    "unsupported_device": "此手柄输入分支暂不支持自动切换。",
    "device_changed": "所选手柄输入已改变，请重新检测后重试。",
    "recovery_identity_changed": "原手柄输入身份已改变，恢复仍待处理。",
    "status_unavailable": "无法确认手柄输入状态，请查看诊断记录。",
    "native_error": "Windows 未能完成手柄输入切换，请查看诊断记录。",
}


def _short_text(value, limit=512):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value.isprintable())


def _error_details(value):
    """Copy only bounded diagnostic scalars; never retain arbitrary service JSON."""
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in _DETAIL_INTS
              if type(value.get(key)) is int and 0 <= value[key] <= 0xffffffff}
    result.update({key: value[key] for key in _DETAIL_BOOLS if type(value.get(key)) is bool})
    for key in ("root_id", "physical_root_id", "hub_id", "new_input_id", "new_input_parent_id", "native_message"):
        if _short_text(value.get(key)):
            result[key] = value[key]
    for key in ("method", "recovery_error"):
        if isinstance(value.get(key), str) and _ERROR_TOKEN.fullmatch(value[key]):
            result[key] = value[key]
    version = value.get("version")
    if _short_text(version, 32) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
        result["version"] = version
    before = value.get("before")
    if isinstance(before, dict):
        snapshot = {key: before[key] for key in ("status", "problem", "flags")
                    if type(before.get(key)) is int and 0 <= before[key] <= 0xffffffff}
        snapshot.update({key: before[key] for key in ("restart_required", "pre_needs_restart", "needs_restart", "started", "healthy")
                         if type(before.get(key)) is bool})
        if snapshot:
            result["before"] = snapshot
    return result


class HotSwitchError(RuntimeError):
    def __init__(self, code, message, *, may_have_restarted=False, details=None):
        self.code = code
        self.may_have_restarted = may_have_restarted
        self.details = _error_details(details)
        super().__init__(message)


def helper_artifact(paths):
    """Find the exact bundled helper, or the exact development build output."""
    assets = as_paths(paths).assets
    packaged = assets / "native" / "hotswitch" / "DSBridgeHotSwitch.exe"
    development = assets / "native" / "hotswitch" / "bin" / "DSBridgeHotSwitch.exe"
    candidates = [packaged] if getattr(sys, "frozen", False) else [development, packaged]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HotSwitchError("helper_missing", "未找到自动热切换组件，请重新取得完整程序包。")


def _wire_request(operation, device_id=None):
    if operation not in ("status", "restart"):
        raise HotSwitchError("invalid_operation", "不支持的热切换请求。")
    request = {"version": API_VERSION, "operation": operation}
    if operation == "restart":
        if not isinstance(device_id, str) or not _DEVICE.fullmatch(device_id):
            raise HotSwitchError("invalid_device", "热切换需要一个精确的实体手柄输入路径。")
        request["device_id"] = device_id
    elif device_id is not None:
        raise HotSwitchError("invalid_request", "状态检查不接受设备路径。")
    if len(json.dumps(request, ensure_ascii=True).encode("ascii")) > 2048:
        raise HotSwitchError("invalid_request", "热切换请求过长。")
    return request


def _reply(data):
    if not isinstance(data, dict) or type(data.get("ok")) is not bool:
        raise HotSwitchError("invalid_reply", "自动热切换组件返回了无效响应。")
    if not data["ok"]:
        code = data.get("code")
        message = data.get("message")
        code = code if isinstance(code, str) and _ERROR_TOKEN.fullmatch(code) else "service_rejected"
        details = _error_details(data.get("details"))
        details.update(_error_details(data))
        if "native_message" not in details and _short_text(message):
            details["native_message"] = message
        # The structured record carries the native wording and numeric results;
        # ordinary UI errors stay short and do not dump device IDs or JSON.
        display = ("Windows 仍要求重启电脑，输入切换尚未完成。" if details.get("restart_required") is True
                   else _NATIVE_MESSAGES.get(code))
        if display is None:
            display = message if _short_text(message) and re.search(r"[\u4e00-\u9fff]", message) else "自动热切换组件未能完成请求，请查看诊断记录。"
        raise HotSwitchError(code, display, details=details)
    return data


def _status_reply(data, owner_sid):
    data = _reply(data)
    if (type(data.get("api_version")) is not int or data["api_version"] != API_VERSION
            or data.get("version") != HELPER_VERSION):
        raise HotSwitchError("version_mismatch", "自动热切换组件版本不匹配，请运行本程序包中的“修复自动切换组件.cmd”。", details=data)
    permitted = data.get("permitted_owner")
    if (not isinstance(permitted, str) or not _SID.fullmatch(permitted)
            or not isinstance(owner_sid, str) or not _SID.fullmatch(owner_sid)
            or permitted.casefold() != owner_sid.casefold()):
        raise HotSwitchError("owner_mismatch", "自动热切换组件属于其他 Windows 用户，当前用户不能使用。")
    if type(data.get("restart_required")) is not bool:
        raise HotSwitchError("invalid_reply", "自动热切换组件未提供有效的重启状态。")
    if data["restart_required"]:
        raise HotSwitchError("restart_required", "Windows 仍要求重启电脑，输入切换尚未完成。", details=data)
    return data


class HotSwitchClient:
    def __init__(self, paths, *, timeout=30.0):
        self.paths = as_paths(paths)
        if not 0.2 <= timeout <= 30:
            raise ValueError("Hot-switch timeout must be between 0.2 and 30 seconds")
        self.timeout = timeout

    def status(self, check_cancel=lambda: None):
        check_cancel()
        owner = current_user_sid()
        data = self._request(_wire_request("status"), check_cancel)
        return _status_reply(data, owner)

    def restart(self, device_id, check_cancel=lambda: None):
        # Validate before any subprocess or service contact.
        request = _wire_request("restart", device_id)
        self.status(check_cancel)
        check_cancel()
        return _reply(self._request(request, check_cancel))

    def _request(self, request, check_cancel):
        if os.name != "nt":
            raise HotSwitchError("windows_required", "自动热切换仅支持 Windows。")
        token = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--hot-switch-worker", token]
        else:
            command = [sys.executable, "-X", "utf8", "-m", __name__, token]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        check_cancel()
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, startupinfo=startup,
                                     creationflags=subprocess.CREATE_NO_WINDOW,
                                     cwd=Path(__file__).resolve().parents[3] if not getattr(sys, "frozen", False) else None)
        except OSError as exc:
            raise HotSwitchError("worker_launch_failed", "无法启动自动热切换通信组件。") from exc
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                check_cancel()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HotSwitchError("service_timeout", "等待自动热切换组件响应超时。",
                                         may_have_restarted=request["operation"] == "restart")
                try:
                    output, _ = child.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            check_cancel()
            if child.returncode:
                raise HotSwitchError("worker_failed", "自动热切换通信组件意外退出。",
                                     may_have_restarted=request["operation"] == "restart")
            if len(output) > _MAX_REPLY:
                raise HotSwitchError("invalid_reply", "自动热切换响应过长。")
            try:
                response = json.loads(output)
            except (ValueError, UnicodeDecodeError) as exc:
                raise HotSwitchError("invalid_reply", "未收到有效的自动热切换响应。") from exc
            return _reply(response)
        finally:
            if child.poll() is None:
                # This is the owned communication worker, never the service,
                # another application, or a PID obtained from device evidence.
                child.kill()
                try:
                    child.communicate(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    raise HotSwitchError("worker_cleanup_pending", "热切换通信组件仍在退出，不能确认本次请求结果。",
                                         may_have_restarted=request["operation"] == "restart") from exc


class _ServiceStatus(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ("service_type", "state", "controls", "win32_exit",
                "service_exit", "checkpoint", "wait_hint", "pid", "flags")]


class _Native:
    def __init__(self):
        if os.name != "nt":
            raise HotSwitchError("windows_required", "自动热切换仅支持 Windows。")
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        self.k = C.WinDLL(str(system / "kernel32.dll"), use_last_error=True)
        self.a = C.WinDLL(str(system / "advapi32.dll"), use_last_error=True)
        for library, name, args, result in (
            (self.k, "GetCurrentProcess", [], C.c_void_p),
            (self.k, "CloseHandle", [C.c_void_p], C.c_int),
            (self.k, "LocalFree", [C.c_void_p], C.c_void_p),
            (self.k, "WaitNamedPipeW", [C.c_wchar_p, C.c_uint32], C.c_int),
            (self.k, "CreateFileW", [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p], C.c_void_p),
            (self.k, "GetNamedPipeServerProcessId", [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int),
            (self.k, "WriteFile", [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            (self.k, "ReadFile", [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            (self.a, "OpenProcessToken", [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_int),
            (self.a, "GetTokenInformation", [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_int),
            (self.a, "ConvertSidToStringSidW", [C.c_void_p, C.POINTER(C.c_void_p)], C.c_int),
            (self.a, "OpenSCManagerW", [C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_void_p),
            (self.a, "OpenServiceW", [C.c_void_p, C.c_wchar_p, C.c_uint32], C.c_void_p),
            (self.a, "QueryServiceStatusEx", [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)], C.c_int),
            (self.a, "CloseServiceHandle", [C.c_void_p], C.c_int),
        ):
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def owner_sid(self):
        token = C.c_void_p()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)):
            raise HotSwitchError("identity_unavailable", "无法确认当前 Windows 用户。")
        try:
            size = C.c_uint32()
            self.a.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not C.sizeof(C.c_void_p) <= size.value <= 65536:
                raise HotSwitchError("identity_unavailable", "无法读取当前用户身份。")
            buffer = C.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token, 1, buffer, len(buffer), C.byref(size)):
                raise HotSwitchError("identity_unavailable", "无法读取当前用户身份。")
            sid = C.c_void_p.from_buffer(buffer).value
            text = C.c_void_p()
            if not self.a.ConvertSidToStringSidW(sid, C.byref(text)):
                raise HotSwitchError("identity_unavailable", "无法解析当前用户身份。")
            try:
                return C.wstring_at(text)
            finally:
                self.k.LocalFree(text)
        finally:
            self.k.CloseHandle(token)

    def service_pid(self):
        manager = self.a.OpenSCManagerW(None, None, 1)
        if not manager:
            raise HotSwitchError("service_access_denied", "无法查询自动热切换服务。")
        try:
            service = self.a.OpenServiceW(manager, SERVICE_NAME, 4)
            if not service:
                code = "service_missing" if C.get_last_error() == 1060 else "service_access_denied"
                raise HotSwitchError(code, "自动热切换服务尚未安装或无法访问，请运行程序包中的“修复自动切换组件.cmd”。")
            try:
                status, size = _ServiceStatus(), C.c_uint32()
                if not self.a.QueryServiceStatusEx(service, 0, C.byref(status), C.sizeof(status), C.byref(size)):
                    raise HotSwitchError("service_access_denied", "无法查询自动热切换服务状态。")
                if status.state != 4 or not status.pid:
                    raise HotSwitchError("service_not_running", "自动热切换服务未运行，请运行程序包中的“修复自动切换组件.cmd”。")
                return status.pid
            finally:
                self.a.CloseServiceHandle(service)
        finally:
            self.a.CloseServiceHandle(manager)

    def request(self, request):
        service_pid = self.service_pid()
        deadline = time.monotonic() + 2.0
        handle = None
        while time.monotonic() < deadline:
            # Exact ACL rights: excludes FILE_CREATE_PIPE_INSTANCE (0x4).
            # SECURITY_IDENTIFICATION allows SID verification only.
            handle = self.k.CreateFileW(PIPE_NAME, 0x0012019B, 0, None, 3, 0x00110000, None)
            if handle not in (None, C.c_void_p(-1).value):
                break
            error = C.get_last_error()
            if error not in (2, 231):
                raise HotSwitchError("pipe_access_denied", "无法连接自动热切换服务。")
            if not self.k.WaitNamedPipeW(PIPE_NAME, 200):
                if C.get_last_error() == 2:
                    time.sleep(0.02)
        else:
            raise HotSwitchError("pipe_unavailable", "自动热切换服务暂未就绪。")
        try:
            server = C.c_uint32()
            if (not self.k.GetNamedPipeServerProcessId(handle, C.byref(server))
                    or server.value != service_pid or self.service_pid() != service_pid):
                raise HotSwitchError("server_identity_mismatch", "自动热切换连接未对应已安装的服务。")
            data = (json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            sent = C.c_uint32()
            if not self.k.WriteFile(handle, data, len(data), C.byref(sent), None) or sent.value != len(data):
                raise HotSwitchError("pipe_write_failed", "无法发送自动热切换请求。")
            chunks = bytearray()
            while len(chunks) <= _MAX_REPLY:
                block, count = C.create_string_buffer(4096), C.c_uint32()
                success = self.k.ReadFile(handle, block, len(block), C.byref(count), None)
                if not success and C.get_last_error() != 234:
                    raise HotSwitchError("pipe_read_failed", "自动热切换连接已中断。")
                if not count.value:
                    raise HotSwitchError("invalid_reply", "自动热切换组件返回了空响应。")
                chunks.extend(block.raw[:count.value])
                if b"\n" in chunks:
                    line, extra = bytes(chunks).split(b"\n", 1)
                    if extra.strip() or len(line) > _MAX_REPLY:
                        raise HotSwitchError("invalid_reply", "自动热切换组件返回了无效响应。")
                    try:
                        return _reply(json.loads(line))
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise HotSwitchError("invalid_reply", "自动热切换组件返回了无效响应。") from exc
            raise HotSwitchError("invalid_reply", "自动热切换响应过长。")
        finally:
            self.k.CloseHandle(handle)


def current_user_sid():
    return _Native().owner_sid()


def _worker_request(token):
    if not isinstance(token, str) or len(token) > 8192:
        raise HotSwitchError("invalid_request", "无效的热切换通信请求。")
    try:
        data = json.loads(base64.b64decode(token, validate=True))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HotSwitchError("invalid_request", "无效的热切换通信请求。") from exc
    if (not isinstance(data, dict) or set(data) - {"version", "operation", "device_id"}
            or type(data.get("version")) is not int or data["version"] != API_VERSION):
        raise HotSwitchError("invalid_request", "无效的热切换通信请求。")
    return _wire_request(data.get("operation"), data.get("device_id"))


def _emit(report):
    data = (json.dumps(report, ensure_ascii=True) + "\n").encode("ascii")
    if sys.stdout is not None:
        sys.stdout.write(data.decode("ascii"))
        sys.stdout.flush()
    elif os.name == "nt":
        # Windowed builds retain their parent's pipe at the Win32 layer.
        kernel = C.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes, kernel.GetStdHandle.restype = [C.c_uint32], C.c_void_p
        kernel.WriteFile.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p]
        kernel.WriteFile.restype = C.c_int
        handle = kernel.GetStdHandle(0xfffffff5)
        if handle not in (None, C.c_void_p(-1).value):
            written = C.c_uint32()
            kernel.WriteFile(handle, data, len(data), C.byref(written), None)


def worker_main(token):
    try:
        request = _worker_request(token)
        report = _Native().request(request)
    except HotSwitchError as exc:
        report = {"ok": False, "code": exc.code, "message": str(exc), "details": _error_details(exc.details)}
    except Exception:
        report = {"ok": False, "code": "worker_failed", "message": "自动热切换通信组件运行失败。"}
    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1] if len(sys.argv) == 2 else ""))
