"""Temporary receiver hiding, owned by a live bridge process, with crash recovery.

The helper holds an actual parent process handle and a cross-build mutex. A
recovery record is flushed before any hiding change. A temporary user-logon
entry recovers an interrupted Windows shutdown and is removed after recovery.
Bluetooth and other applications' hidden devices are never removed.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from dsbridge.platform.windows.hidhide import HidHideControl
from dsbridge.controllers.flydigi.apex6.input_targets import receiver_input_targets
from dsbridge.runtime.paths import as_paths

JOURNAL = "receiver-input-lease.json"
MUTEX = "Local\\OctopusBridge.ReceiverInputLease"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "OctopusBridgeInputRecovery"
TARGET = re.compile(r"^(HID\\VID_37D7&PID_2502&IG_[0-9A-F]{2}|USB\\VID_37D7&PID_2502&(IG_[0-9A-F]{2}|MI_00))\\[^\\]+$", re.I)


def save_json(path, value):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


class Native:
    def __init__(self):
        self.k = C.WinDLL("kernel32.dll", use_last_error=True)
        for name, args, result in (
            ("CreateMutexW", [C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE),
            ("ReleaseMutex", [W.HANDLE], W.BOOL),
            ("CreateEventW", [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            ("SetEvent", [W.HANDLE], W.BOOL),
            ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            ("WaitForMultipleObjects", [W.DWORD, C.POINTER(W.HANDLE), W.BOOL, W.DWORD], W.DWORD),
            ("CloseHandle", [W.HANDLE], W.BOOL),
        ):
            method = getattr(self.k, name)
            method.argtypes, method.restype = args, result

    def event(self, token, suffix):
        handle = self.k.CreateEventW(None, True, False, "Local\\OctopusBridge.Input." + token + suffix)
        if not handle:
            raise C.WinError(C.get_last_error())
        return handle

    def claim(self, timeout=0):
        handle = self.k.CreateMutexW(None, False, MUTEX)
        if not handle:
            raise C.WinError(C.get_last_error())
        if self.k.WaitForSingleObject(handle, timeout) not in (0, 0x80):
            self.k.CloseHandle(handle)
            raise RuntimeError("另一份震动桥正在管理接收器，请先停止它")
        return handle

    def release(self, handle):
        if handle:
            self.k.ReleaseMutex(handle)
            self.k.CloseHandle(handle)

    def close(self, handle):
        if handle:
            self.k.CloseHandle(handle)


def recovery_command(base):
    paths = as_paths(base)
    return subprocess.list2cmdline(paths.command("--recover-receiver-input", executable=paths.assets / "八爪鱼震动桥.exe"))


def recovery_at_logon(base, enabled):
    if not getattr(sys, "frozen", False):
        return
    import winreg
    command = recovery_command(base)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                value, _ = winreg.QueryValueEx(key, RUN_NAME)
                if value == command:
                    winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def restore_record(base, *, control_factory=HidHideControl):
    """Merge out only owned input IDs, including differently cased duplicates."""
    path = Path(base) / JOURNAL
    if not path.exists():
        return False
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("format") != 1:
        raise RuntimeError("接收器输入恢复记录格式异常")
    if not saved.get("restore_pending"):
        recovery_at_logon(base, False)
        return False
    owned = saved["added_devices"]
    if not owned or any(not TARGET.fullmatch(value) for value in owned):
        raise RuntimeError("恢复记录包含非接收器输入设备，未修改配置")
    folded = {value.casefold() for value in owned}
    with control_factory() as driver:
        current = driver.snapshot()
        remaining = [value for value in current["hidden_devices"] if value.casefold() not in folded]
        if remaining != current["hidden_devices"]:
            driver.hidden(remaining)
        if not saved["original_active"] and not remaining and current["active"]:
            driver.active(False)
        after = driver.snapshot()
        if any(value.casefold() in folded for value in after["hidden_devices"]):
            raise RuntimeError("接收器输入隐藏尚未解除")
    saved.update(restore_pending=False, restored_at=now())
    save_json(path, saved)
    recovery_at_logon(base, False)
    return True


def hide_for_session(base, token, parent_pid, *, targets=None, control_factory=HidHideControl):
    targets = receiver_input_targets() if targets is None else targets
    if len(targets) != 3 or any(not TARGET.fullmatch(value) for value in targets):
        raise RuntimeError("接收器输入隔离目标无效")
    with control_factory() as driver:
        current = driver.snapshot()
        if current["inverse"]:
            raise RuntimeError("HidHide 处于反向模式，未修改现有设置")
        folded = {value.casefold() for value in targets}
        existing = {value.casefold() for value in current["hidden_devices"]}
        if existing & folded:
            raise RuntimeError("接收器仍有旧的隐藏设置；请重新打开新版震动桥以自动恢复")
        if not current["active"] and current["hidden_devices"]:
            raise RuntimeError("HidHide 有其他未启用的隐藏项，未改变其他设备的可见性")
        saved = dict(format=1, token=token, parent_pid=parent_pid, restore_pending=True,
                     started_at=now(), original_active=current["active"], added_devices=targets)
        save_json(Path(base) / JOURNAL, saved)
        recovery_at_logon(base, True)
        driver.allow(Path(sys.executable).resolve())
        driver.hidden(current["hidden_devices"] + targets)
        if not current["active"]:
            driver.active(True)
        after = driver.snapshot()
        if not after["active"] or not folded.issubset({value.casefold() for value in after["hidden_devices"]}):
            raise RuntimeError("接收器输入隔离未生效")


def migrate_manual_hiding(base):
    """One-time adoption of this app's old manual setup journal, never all HID."""
    path = Path(base) / "receiver-hidhide-session.json"
    if not path.exists():
        return
    old = json.loads(path.read_text(encoding="utf-8-sig"))
    if old.get("Format") != 1 or not old.get("RestorePending"):
        return
    devices = old.get("AddedDevices", [])
    if any(not TARGET.fullmatch(value) for value in devices):
        raise RuntimeError("旧接收器隐藏记录包含未知设备")
    with HidHideControl() as driver:
        current = driver.snapshot()
        folded = {value.casefold() for value in devices}
        remaining = [value for value in current["hidden_devices"] if value.casefold() not in folded]
        if current["hidden_devices"] != remaining:
            driver.hidden(remaining)
        if not old.get("OriginalActive", True) and not remaining:
            driver.active(False)
    old.update(RestorePending=False, RestoredAt=now(), MigratedTo="automatic-session")
    save_json(path, old)


def recover(base, *, motors=True):
    """Called after a dead parent, or at logon; a live guard prevents takeover."""
    native, claim = Native(), None
    try:
        claim = native.claim()
        return _recover_locked(base, motors=motors)
    finally:
        native.release(claim)


def _recover_locked(base, *, motors):
    from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim
    # Older builds share this waveform mutex but have no input-guard mutex.
    # Do not undo their input hiding while they are still converting.
    output_claim = ReceiverClaim()
    try:
        return _recover_without_output_owner(base, motors=motors)
    finally:
        output_claim.close()


def _recover_without_output_owner(base, *, motors):
    errors = []
    try:
        from dsbridge.platform.windows.usbip import recover_export
        recover_export(base)
    except Exception as exc:
        errors.append("虚拟 USB 恢复：" + str(exc))
    if motors:
        try:
            from dsbridge.controllers.flydigi.apex6.session import recover_pending_motors
            recover_pending_motors(base)
        except Exception as exc:
            errors.append("马达恢复：" + str(exc))
    # Input recovery must not depend on the pad being awake or still connected.
    restored = restore_record(base)
    migrate_manual_hiding(base)
    save_json(Path(base) / "receiver-input-recovery.json", dict(at=now(), restored=restored, warnings=errors))
    return restored


def guard_main(base, token, parent_pid):
    if not re.fullmatch(r"[0-9a-f]{32}", token) or parent_pid <= 0:
        raise ValueError("输入恢复进程参数无效")
    base = Path(base)
    native, claim, parent = Native(), None, None
    ready = native.event(token, ".ready")
    stop = native.event(token, ".stop")
    report_path = base / ("input-guard-" + token + ".json")
    report = dict(token=token, parent_pid=parent_pid, guard_pid=os.getpid(), started_at=now())
    acquired = False
    try:
        parent = native.k.OpenProcess(0x100000, False, parent_pid)  # SYNCHRONIZE, no process control rights.
        if not parent or native.k.WaitForSingleObject(parent, 0) != 258:
            raise RuntimeError("震动桥主进程已经退出")
        claim = native.claim()
        acquired = True
        _recover_locked(base, motors=True)
        handles = (W.HANDLE * 2)(parent, stop)
        if native.k.WaitForMultipleObjects(2, handles, False, 0) != 258:
            raise RuntimeError("启动已取消")
        hide_for_session(base, token, parent_pid)
        report.update(phase="ready", hidden=True)
        save_json(report_path, report)
        native.k.SetEvent(ready)
        reason = native.k.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF)
        if reason not in (0, 1):
            raise C.WinError(C.get_last_error())
        report["reason"] = "parent_exit" if reason == 0 else "normal_stop"
        if reason == 0:
            try:
                from dsbridge.platform.windows.usbip import recover_export
                recover_export(base)
            except Exception as exc:
                report["usb_recovery_warning"] = str(exc)
            # The output owner is gone; restore its volatile motor routing too.
            try:
                from dsbridge.controllers.flydigi.apex6.session import recover_pending_motors
                recover_pending_motors(base)
            except Exception as exc:
                report["motor_recovery_warning"] = str(exc)
            try:
                from dsbridge.platform.windows.audio import repair_audio_defaults
                repair_audio_defaults(base)
            except Exception as exc:
                report["audio_recovery_warning"] = str(exc)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if acquired:
            try:
                restore_record(base)
                report["restored"] = True
            except Exception as exc:
                report["restore_error"] = str(exc)
        report.update(phase="finished", finished_at=now())
        try:
            save_json(report_path, report)
        finally:
            native.k.SetEvent(ready)
            native.release(claim)
            for handle in (parent, ready, stop):
                native.close(handle)
    return 1 if report.get("error") or report.get("restore_error") else 0


class ReceiverInputLease:
    def __init__(self, base):
        self.paths = as_paths(base)
        self.base = self.paths.state
        self.token = uuid.uuid4().hex
        self.process = None
        self.native = Native()
        self.ready = self.stop_event = None
        self.report_path = self.base / ("input-guard-" + self.token + ".json")

    def start(self):
        self.ready = self.native.event(self.token, ".ready")
        self.stop_event = self.native.event(self.token, ".stop")
        args = self.paths.command("--receiver-input-guard", self.token, str(os.getpid()))
        try:
            self.process = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
            handles = (W.HANDLE * 2)(self.ready, int(self.process._handle))
            if self.native.k.WaitForMultipleObjects(2, handles, False, 20000) not in (0, 1):
                raise RuntimeError("接收器自动输入隔离启动超时")
            report = json.loads(self.report_path.read_text(encoding="utf-8"))
            if report.get("phase") != "ready" or self.process.poll() is not None:
                raise RuntimeError(report.get("error") or report.get("restore_error") or "输入恢复进程提前结束")
        except Exception:
            self.stop()
            raise

    @property
    def error(self):
        if self.process is not None and self.process.poll() is not None:
            return RuntimeError("输入恢复进程意外退出，正在停止转换并恢复普通输入")
        return None

    def stop(self):
        try:
            if self.stop_event:
                self.native.k.SetEvent(self.stop_event)
            if self.process is not None:
                self.process.wait(timeout=20)
                report = json.loads(self.report_path.read_text(encoding="utf-8")) if self.report_path.exists() else {}
                if not report.get("restored"):
                    # Only the matching owner may recover after a failed helper.
                    path = self.base / JOURNAL
                    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    if saved.get("token") == self.token:
                        recover(self.base, motors=False)
                self.process = None
        finally:
            for handle in (self.ready, self.stop_event):
                self.native.close(handle)
            self.ready = self.stop_event = None
