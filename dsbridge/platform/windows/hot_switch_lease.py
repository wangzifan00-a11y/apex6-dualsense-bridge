"""Owned, reversible input hiding and refresh for in-game DS attachment.

The guard owns the same mutex as older receiver guards. It records recovery
before any visibility change, keeps a SYNCHRONIZE-only parent process handle,
and delegates only a selected HID ID to the constrained restart service.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from dsbridge.controllers.flydigi.apex6 import recovery as legacy
from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.boot_identity import capture_boot_identity, prove_later_boot
from dsbridge.platform.windows.hidhide import HidHideControl
from dsbridge.platform.windows.hot_switch_service import HotSwitchClient
from dsbridge.platform.windows.input_transition_targets import (InputTargets, resolve_input_targets,
    capture_input_anchor, resolve_current_input, validate_input_anchor)
from dsbridge.runtime.paths import as_paths

Native, save_json, now = legacy.Native, legacy.save_json, legacy.now
TRANSACTION = "hot-switch-transaction.json"
BLUETOOTH_JOURNAL = "controller-input-lease.json"
_TOKEN = re.compile(r"[0-9a-f]{32}")
_BT = r"\{00001124-0000-1000-8000-00805F9B34FB\}"
_HID = re.compile(r"HID\\[^\\\x00-\x20*?]+\\[^\\\x00-\x20*?]+", re.I)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else None


def _journal_name(targets):
    return legacy.JOURNAL if targets.transport in ("usb", "receiver") else BLUETOOTH_JOURNAL


def _same_targets(left, right):
    return (left.transport == right.transport
            and left.device_id.casefold() == right.device_id.casefold()
            and left.restart_id.casefold() == right.restart_id.casefold()
            and tuple(v.casefold() for v in left.hidden_ids) == tuple(v.casefold() for v in right.hidden_ids)
            and tuple(v.casefold() for v in left.device_ids) == tuple(v.casefold() for v in right.device_ids))


def _same_root(left, right):
    return left.transport == right.transport and left.restart_id.casefold() == right.restart_id.casefold()


def _current_targets(record):
    return _saved_targets(record.get("current_targets", record["targets"]))


def _restart_selected(client, targets, check_cancel=lambda: None):
    result = client.restart(targets.device_id, check_cancel)
    if (not isinstance(result, dict) or result.get("ok") is not True or result.get("code") != "restarted"
            or not isinstance(result.get("root_id"), str)
            or result["root_id"].casefold() != targets.restart_id.casefold()):
        raise RuntimeError("热切换组件未确认所选输入分支已刷新")
    return result


def _saved_targets(value):
    """Validate recovery scope even while the physical device is unplugged.

    This validates only what may be removed from our own hide list. Actual
    restart always requires the service's independent live-tree validation.
    """
    if not isinstance(value, dict):
        raise RuntimeError("热切换设备恢复记录无效")
    device, root, transport = (value.get(key) for key in ("device_id", "restart_id", "transport"))
    hidden, devices = value.get("hidden_ids"), value.get("device_ids")
    if (not isinstance(device, str) or not _HID.fullmatch(device) or not isinstance(root, str)
            or not isinstance(hidden, list) or not isinstance(devices, list)
            or not hidden or len(hidden) > 3 or any(not isinstance(v, str) for v in hidden + devices)
            or [v.casefold() for v in hidden] != [v.casefold() for v in devices]
            or len({v.casefold() for v in hidden}) != len(hidden)
            or hidden[0].casefold() != device.casefold() or hidden[-1].casefold() != root.casefold()):
        raise RuntimeError("热切换恢复记录包含未验证的输入分支")
    apex = re.fullmatch(r"HID\\VID_37D7&PID_2502&IG_([0-9A-F]{2})\\[^\\\x00-\x20*?]+", device, re.I)
    if transport in ("usb", "receiver") and apex:
        valid = (len(hidden) == 3 and re.fullmatch(r"USB\\VID_37D7&PID_2502&IG_" + apex[1] + r"\\[^\\\x00-\x20*?]+", hidden[1], re.I)
                 and re.fullmatch(r"USB\\VID_37D7&PID_2502&MI_00\\[^\\\x00-\x20*?]+", root, re.I))
    elif transport == "bluetooth":
        pair = re.search(r"VID[_&](?:[0-9A-F]{4})?([0-9A-F]{4})[_&]PID[_&]([0-9A-F]{4})", device, re.I)
        valid = (len(hidden) == 2 and pair and re.fullmatch(
            r"BTHENUM\\" + _BT + r"_VID&[0-9A-F]{4}" + pair[1] + r"_PID&" + pair[2] + r"\\[^\\\x00-\x20*?]+", root, re.I))
    else:
        valid = False
    if not valid:
        raise RuntimeError("恢复记录不能包含蓝牙适配器、复合设备或未知输入分支")
    return InputTargets(device, root, tuple(hidden), tuple(devices), transport)


def recovery_command(paths):
    paths = as_paths(paths)
    return subprocess.list2cmdline(paths.command("--recover-hot-switch", executable=paths.assets / "八爪鱼震动桥.exe"))


def _command_argv(command):
    """Use Windows command-line parsing, never shell tokenization or execution."""
    if not isinstance(command, str) or not command or len(command) > 32767 or "\0" in command:
        return []
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    shell = C.WinDLL(str(system / "shell32.dll"), use_last_error=True)
    kernel = C.WinDLL(str(system / "kernel32.dll"), use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [W.LPCWSTR, C.POINTER(C.c_int)]
    shell.CommandLineToArgvW.restype = C.POINTER(W.LPWSTR)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p], C.c_void_p
    count = C.c_int()
    arguments = shell.CommandLineToArgvW(command, C.byref(count))
    if not arguments:
        raise C.WinError(C.get_last_error())
    try:
        return [arguments[index] for index in range(count.value)] if count.value == 4 else []
    finally:
        kernel.LocalFree(C.cast(arguments, C.c_void_p))


def _recovery_args_match(paths, arguments, *, require_executable):
    if (len(arguments) != 4 or any(not isinstance(value, str) or not value or "\0" in value for value in arguments)
            or arguments[1] != "--data-dir" or arguments[3] != "--recover-hot-switch"):
        return False
    executable, state = Path(arguments[0]), Path(arguments[2])
    if (not executable.is_absolute() or not state.is_absolute()
            or executable.name.casefold() != "八爪鱼震动桥.exe"):
        return False
    if require_executable and not executable.is_file():
        return False
    return os.path.normcase(str(state.resolve())) == os.path.normcase(str(paths.state.resolve()))


def _owns_recovery_command(paths, record, command):
    saved = record.get("recovery_command")
    if saved is not None and (not isinstance(saved, str) or command != saved):
        return False
    try:
        # Old records have no saved command. Adopt only the exact bridge CLI,
        # for this data directory, with an existing executable. A saved command
        # remains owned even after the old build has been moved or deleted.
        return _recovery_args_match(paths, _command_argv(command), require_executable=saved is None)
    except (OSError, ValueError):
        return False


def recovery_at_logon(paths, enabled):
    if not getattr(sys, "frozen", False):
        return
    import winreg
    paths = as_paths(paths)
    record = _load_transaction(paths)
    if record is None:
        if enabled:
            raise RuntimeError("缺少恢复事务，未登记临时登录入口")
        return
    command = recovery_command(paths)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, legacy.RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            existing, value_type = winreg.QueryValueEx(key, legacy.RUN_NAME)
        except FileNotFoundError:
            existing, value_type = None, None
        if enabled:
            if existing is not None:
                if value_type != winreg.REG_SZ or not _owns_recovery_command(paths, record, existing):
                    raise RuntimeError("临时登录入口属于其他恢复会话，未覆盖现有设置")
                if "recovery_command" not in record:
                    record["recovery_command"] = existing
                    save_json(paths.state / TRANSACTION, record)
                return  # Preserve the registered command across build changes.
            record["recovery_command"] = command
            save_json(paths.state / TRANSACTION, record)
            winreg.SetValueEx(key, legacy.RUN_NAME, 0, winreg.REG_SZ, command)
        elif value_type == winreg.REG_SZ and _owns_recovery_command(paths, record, existing):
            winreg.DeleteValue(key, legacy.RUN_NAME)


def _load_transaction(paths):
    record = _read(paths.state / TRANSACTION)
    if record is None:
        return None
    if (not isinstance(record, dict) or record.get("format") != 1
            or not isinstance(record.get("token"), str) or not _TOKEN.fullmatch(record["token"])
            or type(record.get("parent_pid")) is not int or record["parent_pid"] <= 0
            or type(record.get("original_active")) is not bool
            or type(record.get("restart_needed")) is not bool
            or type(record.get("restore_pending")) is not bool
            or (not record["restore_pending"] and record["restart_needed"])):
        raise RuntimeError("热切换恢复记录格式异常，未修改输入设置")
    targets = _saved_targets(record.get("targets"))
    if (not isinstance(record.get("device_id"), str)
            or record["device_id"].casefold() != targets.device_id.casefold()
            or record.get("journal") != _journal_name(targets)):
        raise RuntimeError("热切换恢复记录的设备不一致")
    current = _current_targets(record)
    history = record.get("target_history", [record["targets"]])
    if not isinstance(history, list) or not 1 <= len(history) <= 8:
        raise RuntimeError("热切换输入实例恢复记录无效")
    versions = [_saved_targets(value) for value in history]
    if (not _same_root(targets, current) or any(not _same_root(targets, value) for value in versions)
            or not any(_same_targets(targets, value) for value in versions)
            or not any(_same_targets(current, value) for value in versions)):
        raise RuntimeError("热切换输入实例不属于原来的输入根节点")
    if record.get("input_anchor") is not None:
        validate_input_anchor(targets, record["input_anchor"])
    elif len(versions) != 1 or not _same_targets(targets, current):
        raise RuntimeError("缺少物理连接记录，未信任已变更的输入实例")
    if (record.get("usb_port_index") is not None
            and (type(record["usb_port_index"]) is not int or not 1 <= record["usb_port_index"] <= 255)):
        raise RuntimeError("热切换 USB 端口恢复记录无效")
    owned = record.get("added_devices")
    if (not isinstance(owned, list) or any(not isinstance(v, str) for v in owned)
            or len({v.casefold() for v in owned}) != len(owned)
            or not {v.casefold() for v in owned}.issubset({v.casefold() for version in versions for v in version.hidden_ids})):
        raise RuntimeError("热切换恢复记录包含非本次拥有的隐藏项")
    return record


def _parent_is_live(native, pid):
    handle = native.k.OpenProcess(0x100000, False, pid)
    if not handle:
        if C.get_last_error() == 87:  # The PID no longer exists.
            return False
        raise RuntimeError("无法核实旧震动桥是否已退出，未接管输入设置")
    try:
        result = native.k.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise RuntimeError("无法核实旧震动桥的运行状态")
        return result == 258
    finally:
        native.close(handle)


def _guard_finished(paths, native, record):
    """A GUI outlives its Engine; only a matching, exited guard retires ownership.

    The caller already owns the common input mutex. A missing/starting/ready
    guard report, or a reused still-live guard PID, cannot authorize takeover.
    Output-export ownership is checked separately even for retired input guards.
    """
    report = _read(paths.state / ("input-guard-" + record["token"] + ".json"))
    if (not isinstance(report, dict) or report.get("token") != record["token"]
            or report.get("parent_pid") != record["parent_pid"] or report.get("phase") != "finished"
            or type(report.get("guard_pid")) is not int or report["guard_pid"] <= 0
            or report["guard_pid"] == record["parent_pid"]):
        return False
    return not _parent_is_live(native, report["guard_pid"])


def _check_output_owner(paths, native=None):
    """Older Bluetooth builds do not hold either receiver mutex."""
    record = _read(paths.state / "viiper-usb-session.json")
    if record is None:
        return
    if not isinstance(record, dict) or record.get("format") != 1:
        raise RuntimeError("虚拟 USB 恢复记录格式异常，未接管输出")
    if not record.get("restore_pending"):
        return
    pid = record.get("process_id")
    if type(pid) is not int or pid <= 0:
        raise RuntimeError("虚拟 USB 恢复记录缺少有效的进程身份")
    if _parent_is_live(native or Native(), pid):
        raise RuntimeError("旧震动桥仍在管理虚拟 DS，未接管其输出")


def _recover_outputs(paths):
    warnings = []
    try:
        _check_output_owner(paths)
    except Exception as exc:
        # During parent-exit cleanup, never let another live owner's export or
        # audio defaults get changed, but still restore our own input hiding.
        return ["输出所有者核查：" + str(exc)]
    actions = (("虚拟 USB", "dsbridge.platform.windows.usbip", "recover_export"),
               ("马达", "dsbridge.controllers.flydigi.apex6.session", "recover_pending_motors"),
               ("音频", "dsbridge.platform.windows.audio", "repair_audio_defaults"))
    for name, module, function in actions:
        try:
            getattr(importlib.import_module(module), function)(paths)
        except Exception as exc:
            warnings.append(name + "恢复：" + str(exc))
    return warnings


def _optional_boot_identity():
    try:
        return capture_boot_identity()
    except Exception:
        # Boot evidence is an optional recovery shortcut, never a reason to
        # invent an identity or prevent the normal device-refresh path.
        return None


def _begin_hiding(paths, token, parent_pid, targets):
    """Persist both ownership records and the logon recovery entry before I/O."""
    boot_identity = _optional_boot_identity()
    anchor = capture_input_anchor(targets)
    with HidHideControl() as driver:
        current = driver.snapshot()
        if current["inverse"]:
            raise RuntimeError("HidHide 处于反向模式，未修改现有设置")
        target_ids = {v.casefold() for v in targets.hidden_ids}
        existing = {v.casefold() for v in current["hidden_devices"]}
        if not current["active"] and existing - target_ids:
            raise RuntimeError("HidHide 有其他未启用的隐藏项，未改变其他设备的可见性")
        owned = [v for v in targets.hidden_ids if v.casefold() not in existing]
        record = dict(format=1, token=token, parent_pid=parent_pid, started_at=now(),
                      device_id=targets.device_id, targets=targets.to_dict(), kind=targets.transport,
                      journal=_journal_name(targets), phase="prepared", restart_needed=False,
                      restore_pending=True, original_active=current["active"], added_devices=owned,
                      input_visibility_restored=False, refresh_pending=False,
                      recovery_command=recovery_command(paths), boot_identity=boot_identity,
                      input_anchor=anchor, current_targets=targets.to_dict(), target_history=[targets.to_dict()])
        save_json(paths.state / TRANSACTION, record)
        journal = dict(format=1, token=token, parent_pid=parent_pid, started_at=record["started_at"],
                       restore_pending=True, original_active=current["active"], added_devices=owned,
                       hot_switch=True)
        save_json(paths.state / record["journal"], journal)
        recovery_at_logon(paths, True)
        driver.allow(Path(sys.executable).resolve())
        if owned:
            driver.hidden(current["hidden_devices"] + owned)
        if not current["active"]:
            driver.active(True)
        after = driver.snapshot()
        if after["inverse"] or not after["active"] or not {v.casefold() for v in targets.hidden_ids}.issubset({v.casefold() for v in after["hidden_devices"]}):
            raise RuntimeError("热切换的实体输入隔离未生效")
    record["phase"] = "hidden"
    save_json(paths.state / TRANSACTION, record)
    return record


def _adopt_current(paths, record, current, *, hide_new=False):
    original = _saved_targets(record["targets"])
    if not _same_root(original, current):
        raise RuntimeError("重新连接的手柄不属于原输入根节点")
    history = list(record.get("target_history", [record["targets"]]))
    if not any(_same_targets(_saved_targets(value), current) for value in history):
        if len(history) >= 8:
            raise RuntimeError("手柄输入实例变化过多，已停止自动接管")
        history.append(current.to_dict())
    record.update(current_targets=current.to_dict(), target_history=history)
    if not hide_new:
        save_json(paths.state / TRANSACTION, record)
        return
    with HidHideControl() as driver:
        snapshot = driver.snapshot()
        if snapshot["inverse"] or not snapshot["active"]:
            raise RuntimeError("重新连接时输入隔离设置发生变化")
        existing = {value.casefold() for value in snapshot["hidden_devices"]}
        added = [value for value in current.hidden_ids if value.casefold() not in existing]
        already_owned = {value.casefold() for value in record["added_devices"]}
        record["added_devices"] += [value for value in added if value.casefold() not in already_owned]
        # Journal every generation before hiding its newly enumerated HID/IG.
        # The stable MI_00 remains hidden, but is not assumed to cover raw HID.
        save_json(paths.state / TRANSACTION, record)
        path = paths.state / record["journal"]
        journal = _read(path)
        if not isinstance(journal, dict) or journal.get("token") != record["token"]:
            raise RuntimeError("重新连接时输入隔离恢复记录已改变")
        journal.update(added_devices=list(record["added_devices"]), restore_pending=True)
        save_json(path, journal)
        if added:
            driver.hidden(snapshot["hidden_devices"] + added)
        after = driver.snapshot()
        if (after["inverse"] or not after["active"]
                or not {value.casefold() for value in current.hidden_ids}.issubset({v.casefold() for v in after["hidden_devices"]})):
            raise RuntimeError("重新连接后的实体输入隔离未生效")


def _resolve_record_current(paths, record, *, hide_new=False):
    current = _current_targets(record)
    if record.get("input_anchor") is not None:
        current = resolve_current_input(current, record["input_anchor"])
        _adopt_current(paths, record, current, hide_new=hide_new)
    return current


def _adopt_restart_reply(paths, record, result, *, hide_new=False):
    original = _saved_targets(record["targets"])
    previous = _current_targets(record)
    new_id = result.get("new_input_id", previous.device_id)
    if not isinstance(new_id, str) or not _HID.fullmatch(new_id):
        raise RuntimeError("热切换组件未返回有效的当前手柄输入")
    anchor = record.get("input_anchor")
    if anchor is not None:
        current = resolve_current_input(previous, anchor)
    elif result.get("method") == "usb_hub_cycle_port":
        # Legacy records have no physical anchor. The protected service alone
        # can resolve an absent old HID through its last validated USB journal.
        # Accept that result only after reading back the exact old MI_00 and
        # the service's physical parent/hub on the current complete input tree.
        current = resolve_input_targets(ControllerConnection(kind=original.transport, device_id=new_id))
        if not _same_root(original, current):
            raise RuntimeError("旧恢复记录返回了不同的手柄输入根节点")
        anchor = capture_input_anchor(current)
    else:
        current = previous
    if current.device_id.casefold() != new_id.casefold() or not _same_root(original, current):
        raise RuntimeError("热切换组件与当前实体输入核查结果不一致")
    if result.get("method") == "usb_hub_cycle_port":
        port = result.get("port_index")
        if (anchor is None or type(port) is not int or not 1 <= port <= 255
                or not isinstance(result.get("physical_root_id"), str)
                or result["physical_root_id"].casefold() != anchor["physical_root_id"].casefold()
                or not isinstance(result.get("hub_id"), str) or result["hub_id"].casefold() != anchor["hub_id"].casefold()
                or not isinstance(result.get("new_input_parent_id"), str)
                or result["new_input_parent_id"].casefold() != current.device_ids[1].casefold()
                or (record.get("usb_port_index") is not None and record["usb_port_index"] != port)):
            raise RuntimeError("热切换后的物理 USB 端口或输入身份不一致")
        record["usb_port_index"] = port
    record["input_anchor"] = anchor
    _adopt_current(paths, record, current, hide_new=hide_new)
    return current


def _restart_record(paths, client, record, check_cancel=lambda: None, *, hide_new=False):
    current = _resolve_record_current(paths, record, hide_new=hide_new)
    result = _restart_selected(client, current, check_cancel)
    _adopt_restart_reply(paths, record, result, hide_new=hide_new)
    return result


def _switch_isolated(paths, client, record, check_cancel=lambda: None):
    """Converge in at most two successful refreshes; never retry a failed I/O.

    HidHide gates file creation, not reads from existing handles. A new HID
    instance hidden after enumeration therefore needs one more refresh. Only
    identities hidden BEFORE the final refresh establish this timing property;
    it is not a claim to filter Windows Raw Input or privileged consumers.
    """
    isolation = dict(converged=False, rounds=0, hidden_before_last_refresh=[],
                     final_hidden_ids=[], final_input_id="", evidence=[])
    record["isolation"] = isolation
    for number in (1, 2):
        check_cancel()
        current = _resolve_record_current(paths, record, hide_new=True)
        known = {value.casefold() for version in record["target_history"] for value in version["hidden_ids"]}
        # Keep the exclusive HidHide control handle across the native refresh,
        # so another configuration writer cannot remove the recorded pre-hide.
        with HidHideControl() as driver:
            before = driver.snapshot()
            pre_hidden = [value for value in before["hidden_devices"] if value.casefold() in known]
            if (before["inverse"] or not before["active"]
                    or not {v.casefold() for v in current.hidden_ids}.issubset({v.casefold() for v in pre_hidden})):
                raise RuntimeError("自动切换未完成：刷新前的实体输入隐藏状态未通过检查")
            evidence = dict(round=number, active=before["active"], inverse=before["inverse"],
                            pre_hidden=pre_hidden, current=[], input_id="", verified=False, native_success=False)
            isolation.update(rounds=number, hidden_before_last_refresh=pre_hidden,
                             final_hidden_ids=[], final_input_id="")
            isolation["evidence"].append(evidence)
            record.update(phase="switching", restart_needed=True)
            save_json(paths.state / TRANSACTION, record)
            check_cancel()
            result = _restart_selected(client, current, check_cancel)
            evidence["native_success"] = True
            save_json(paths.state / TRANSACTION, record)
            after = driver.snapshot()
            if (after["inverse"] or not after["active"]
                    or not {v.casefold() for v in pre_hidden}.issubset({v.casefold() for v in after["hidden_devices"]})):
                raise RuntimeError("自动切换未完成：刷新期间的实体输入隐藏状态已改变")
        # A successful native response must also match the same physical anchor.
        # Every newly observed ID is journalled before it is hidden, including
        # on the final unsuccessful round, so cleanup always owns exact paths.
        current = _adopt_restart_reply(paths, record, result, hide_new=True)
        verified = {v.casefold() for v in current.hidden_ids}.issubset({v.casefold() for v in pre_hidden})
        evidence.update(current=list(current.hidden_ids), input_id=current.device_id, verified=verified)
        isolation.update(converged=verified, final_hidden_ids=list(current.hidden_ids),
                         final_input_id=current.device_id)
        save_json(paths.state / TRANSACTION, record)
        check_cancel()
        if verified:
            return result
    raise RuntimeError("自动切换未完成：两次刷新后手柄仍产生新的输入实例，已停止桥接并恢复实体输入")


def _read_reboot_input(targets):
    """Verify the same live input and readable XInput without sending output."""
    if not _same_targets(resolve_input_targets(ControllerConnection(kind=targets.transport, device_id=targets.device_id)), targets):
        raise RuntimeError("重启后的手柄输入分支与恢复记录不一致")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    cfg = C.WinDLL(str(system / "cfgmgr32.dll"))
    uint = C.c_uint32
    cfg.CM_Locate_DevNodeW.argtypes, cfg.CM_Locate_DevNodeW.restype = [C.POINTER(uint), W.LPCWSTR, uint], uint
    cfg.CM_Get_DevNode_Status.argtypes, cfg.CM_Get_DevNode_Status.restype = [C.POINTER(uint), C.POINTER(uint), uint, uint], uint
    node, status, problem = uint(), uint(), uint()
    if (cfg.CM_Locate_DevNodeW(C.byref(node), targets.restart_id, 0)
            or cfg.CM_Get_DevNode_Status(C.byref(status), C.byref(problem), node, 0)
            or not status.value & 0x8 or status.value & (0x100 | 0x400)):
        raise RuntimeError("重启后的手柄输入尚未正常启动，或 Windows 仍要求重启")
    pair = re.search(r"VID[_&](?:[0-9A-F]{4})?([0-9A-F]{4})[_&]PID[_&]([0-9A-F]{4})", targets.device_id, re.I)
    if not pair:
        raise RuntimeError("无法核对重启后的实体手柄身份")
    connection = ControllerConnection(kind=targets.transport, device_id=targets.device_id,
                                      vendor_id=int(pair[1], 16), product_id=int(pair[2], 16))
    from dsbridge.platform.windows.xinput import XInput
    source = XInput()
    try:
        slot = source.rebind(connection)
        if slot is None or source.get_state(slot) is None:
            raise RuntimeError("重启后的同一只手柄尚未恢复普通输入")
    finally:
        source.close()
    return dict(root_id=targets.restart_id, status=status.value, problem=problem.value,
                input_readable=True)


def _check_reboot_outputs(paths):
    for filename in ("viiper-usb-session.json", "receiver-session.json"):
        saved = _read(paths.state / filename)
        if saved is not None and (not isinstance(saved, dict) or saved.get("restore_pending") is not False):
            raise RuntimeError("输出恢复记录尚未完成，未跳过设备刷新")
    from dsbridge.platform.windows.audio import WindowsAudio, is_sony_audio
    with WindowsAudio() as audio:
        defaults = audio.snapshot()["defaults"]
    if any(is_sony_audio(endpoint) for endpoint in defaults.values()):
        raise RuntimeError("默认音频仍指向虚拟 DS，尚未完成重启恢复")


def _restore_transaction(paths, client, *, expected_token=None, outputs_restored=False):
    record = _load_transaction(paths)
    if record is None or (expected_token is not None and record["token"] != expected_token):
        return dict(restored=True, input_visibility_restored=True, refresh_pending=False)
    if not record["restore_pending"]:
        recovery_at_logon(paths, False)
        return dict(restored=True, input_visibility_restored=True, refresh_pending=False)
    journal_path = paths.state / record["journal"]
    journal = _read(journal_path)
    if journal is not None:
        if not isinstance(journal, dict) or journal.get("format") != 1:
            raise RuntimeError("热切换的输入恢复记录已被其他会话替换")
        if journal.get("token") != record["token"]:
            # A crash can occur after writing the new transaction but before
            # replacing the previous, already completed compatibility journal.
            if journal.get("restore_pending") is not False:
                raise RuntimeError("热切换的输入恢复记录已被其他会话替换")
            journal = None
    owned = {value.casefold() for value in record["added_devices"]}
    with HidHideControl() as driver:
        current = driver.snapshot()
        remaining = [v for v in current["hidden_devices"] if v.casefold() not in owned]
        visibility_changed = remaining != current["hidden_devices"]
        if remaining != current["hidden_devices"]:
            driver.hidden(remaining)
        selected = {v.casefold() for version in record.get("target_history", [record["targets"]]) for v in version["hidden_ids"]}
        restore_inactive = not record["original_active"] and not ({v.casefold() for v in remaining} - selected)
        if restore_inactive and current["active"]:
            visibility_changed = True
            driver.active(False)
        after = driver.snapshot()
        if any(v.casefold() in owned for v in after["hidden_devices"]):
            raise RuntimeError("热切换添加的输入隐藏项尚未解除")
        if restore_inactive and after["active"]:
            raise RuntimeError("HidHide 原始启用状态尚未恢复")
    if (visibility_changed or record.get("input_visibility_restored") is not True
            or not record.get("visibility_restored_boot_identity")):
        # Only a reboot AFTER visibility was restored retires all consumers of
        # that configuration. Unhiding for the first time after reboot still
        # needs the normal refresh; the original transaction epoch is too old.
        record["visibility_restored_boot_identity"] = _optional_boot_identity()
    record.update(input_visibility_restored=True, phase="restoring", updated_at=now())
    # The legacy journal describes visibility only. A separate transaction keeps
    # a failed refresh pending without making old code replay a completed unhide.
    if journal is not None:
        journal.update(restore_pending=False, restored_at=now())
        save_json(journal_path, journal)
    save_json(paths.state / TRANSACTION, record)
    result = dict(input_visibility_restored=True, refresh_pending=record["restart_needed"])
    if record["restart_needed"]:
        try:
            targets = _current_targets(record)
            evidence = prove_later_boot(record.get("visibility_restored_boot_identity"))
            if evidence:
                if not outputs_restored:
                    raise RuntimeError("重启已确认，但输出恢复尚未通过检查")
                service = client.status()
                if service.get("restart_required") is not False or service.get("recovery_pending") is not False:
                    raise RuntimeError("热切换组件仍有待恢复状态，未跳过设备刷新")
                targets = _resolve_record_current(paths, record)
                health = _read_reboot_input(targets)
                _check_reboot_outputs(paths)
                result["refresh_result"] = dict(ok=True, code="restored_after_reboot",
                                                method="system_reboot", **health, **evidence)
            else:
                result["refresh_result"] = _restart_record(paths, client, record)
            record["restart_needed"] = False
            record.pop("refresh_error", None)
            record.pop("refresh_error_details", None)
            result["refresh_pending"] = False
        except Exception as exc:
            result["refresh_error"] = str(exc)
            result["refresh_error_details"] = dict(getattr(exc, "details", {}))
    result["restored"] = not result["refresh_pending"]
    result["current_targets"] = _current_targets(record).to_dict()
    record.update(result, restore_pending=not result["restored"], updated_at=now(),
                  phase="finished" if result["restored"] else "refresh_pending")
    save_json(paths.state / TRANSACTION, record)
    recovery_at_logon(paths, not result["restored"])
    return result


def _recover_locked(paths, native, client, *, expected_token=None, recover_outputs=True):
    record = _load_transaction(paths)
    later_boot = prove_later_boot(record.get("boot_identity")) if record and record["restore_pending"] else None
    if record and record["restore_pending"] and record["token"] != expected_token:
        if (not later_boot and _parent_is_live(native, record["parent_pid"])
                and not _guard_finished(paths, native, record)):
            raise RuntimeError("旧震动桥仍在运行，未接管其输入恢复记录")
    old = _read(paths.state / legacy.JOURNAL) if record is None or not record["restore_pending"] else None
    if old is not None:
        if not isinstance(old, dict):
            raise RuntimeError("旧输入恢复记录格式异常，未修改输入设置")
        if old.get("restore_pending"):
            if old.get("hot_switch"):
                raise RuntimeError("缺少热切换事务记录，未猜测输入恢复范围")
            pid = old.get("parent_pid")
            if type(pid) is not int or pid <= 0:
                raise RuntimeError("旧输入恢复记录缺少有效的进程身份")
            if _parent_is_live(native, pid):
                raise RuntimeError("旧震动桥仍在运行，未接管其输入设置")
    if recover_outputs:
        _check_output_owner(paths, native)
    # Old builds without an input guard still hold this waveform-owner mutex.
    from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim
    output_claim = ReceiverClaim()
    try:
        warnings = _recover_outputs(paths) if recover_outputs else []
        result = _restore_transaction(paths, client, expected_token=expected_token,
                                      outputs_restored=recover_outputs and not warnings)
        if record is None or not record["restore_pending"]:
            if old and old.get("restore_pending"):
                legacy.restore_record(paths)
            legacy.migrate_manual_hiding(paths)
        result["warnings"] = warnings
        save_json(paths.state / "hot-switch-recovery.json", dict(at=now(), **result))
        return result
    finally:
        output_claim.close()


def recover(paths):
    paths = as_paths(paths)
    native, claim = Native(), None
    try:
        claim = native.claim()
        return _recover_locked(paths, native, HotSwitchClient(paths))
    finally:
        native.release(claim)


def _request_targets(paths, token, parent_pid):
    request = _read(paths.state / ("hot-switch-request-" + token + ".json"))
    if (not isinstance(request, dict) or request.get("format") != 1 or request.get("token") != token
            or request.get("parent_pid") != parent_pid or not isinstance(request.get("connection"), dict)):
        raise RuntimeError("热切换启动请求无效")
    allowed = ControllerConnection.__dataclass_fields__
    connection = ControllerConnection(**{key: value for key, value in request["connection"].items() if key in allowed})
    expected = _saved_targets(request.get("targets"))
    actual = resolve_input_targets(connection)
    if not _same_targets(actual, expected):
        raise RuntimeError("所选手柄的输入分支已经改变，已取消热切换")
    return actual


def guard_main(paths, token, parent_pid):
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or type(parent_pid) is not int or parent_pid <= 0:
        raise ValueError("热切换输入恢复进程参数无效")
    paths = as_paths(paths)
    native, claim, parent, ready, stop = Native(), None, None, None, None
    client = HotSwitchClient(paths)
    report_path = paths.state / ("input-guard-" + token + ".json")
    report = dict(token=token, parent_pid=parent_pid, guard_pid=os.getpid(), started_at=now(), phase="starting")
    acquired, record = False, None
    try:
        ready, stop = native.event(token, ".ready"), native.event(token, ".stop")
        parent = native.k.OpenProcess(0x100000, False, parent_pid)
        if not parent or native.k.WaitForSingleObject(parent, 0) != 258:
            raise RuntimeError("震动桥主进程已经退出")
        handles = (W.HANDLE * 2)(parent, stop)

        def check_cancel():
            if native.k.WaitForMultipleObjects(2, handles, False, 0) != 258:
                raise RuntimeError("热切换启动已取消")

        claim = native.claim()
        acquired = True
        previous = _recover_locked(paths, native, client)
        if not previous["restored"]:
            raise RuntimeError("上次热切换的设备刷新仍待恢复：" + previous.get("refresh_error", "设备尚未恢复"))
        check_cancel()
        targets = _request_targets(paths, token, parent_pid)
        report["targets"] = targets.to_dict()
        report["service"] = client.status(check_cancel)
        check_cancel()
        record = _begin_hiding(paths, token, parent_pid, targets)
        check_cancel()
        # A timeout/lost response can follow a successful restart. Keep refresh
        # recovery pending before asking the service to perform the operation.
        record.update(phase="switching", restart_needed=True)
        save_json(paths.state / TRANSACTION, record)
        restarted = _switch_isolated(paths, client, record, check_cancel)
        check_cancel()
        record.update(phase="ready", switch_result=restarted)
        save_json(paths.state / TRANSACTION, record)
        report.update(phase="ready", hidden=True, restart_result=restarted,
                      current_targets=record["current_targets"], input_anchor=record["input_anchor"],
                      isolation=record["isolation"])
        save_json(report_path, report)
        native.k.SetEvent(ready)
        reason = native.k.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF)
        if reason not in (0, 1):
            raise RuntimeError("热切换恢复进程等待主程序时出现错误")
        report["reason"] = "parent_exit" if reason == 0 else "normal_stop"
    except Exception as exc:
        report["error"] = str(exc)
        report["error_details"] = dict(getattr(exc, "details", {}))
        if record is not None and "isolation" in record:
            report["isolation"] = record["isolation"]
    finally:
        if acquired:
            if parent and native.k.WaitForSingleObject(parent, 0) == 0:
                report["reason"] = "parent_exit"
                report["recovery_warnings"] = _recover_outputs(paths)
            try:
                report.update(_restore_transaction(paths, client, expected_token=token))
            except Exception as exc:
                report.update(restored=False, restore_error=str(exc))
        report.update(phase="finished", finished_at=now())
        try:
            save_json(report_path, report)
        finally:
            if ready:
                native.k.SetEvent(ready)
            native.release(claim)
            for handle in (parent, ready, stop):
                native.close(handle)
    return 1 if report.get("error") or not report.get("restored") else 0


class HotSwitchLease:
    def __init__(self, paths, connection, targets=None):
        self.paths = as_paths(paths)
        self.base = self.paths.state
        self.connection = connection
        self.targets = targets
        self.token = uuid.uuid4().hex
        self.native = Native()
        self.process = self.ready = self.stop_event = None
        self.report_path = self.base / ("input-guard-" + self.token + ".json")
        self._last_error = None
        self._current_connection = connection

    @property
    def current_connection(self):
        return self._current_connection

    def start_cancellable(self, check_cancel):
        if self.process is not None:
            raise RuntimeError("热切换输入会话已经启动")
        check_cancel()
        self.targets = self.targets or resolve_input_targets(self.connection)
        expected = _saved_targets(self.targets.to_dict())
        request = dict(format=1, token=self.token, parent_pid=os.getpid(), connection=self.connection.to_dict(),
                       targets=expected.to_dict(), at=now())
        save_json(self.base / ("hot-switch-request-" + self.token + ".json"), request)
        try:
            self.ready = self.native.event(self.token, ".ready")
            self.stop_event = self.native.event(self.token, ".stop")
            self.process = subprocess.Popen(self.paths.command("--hot-switch-input-guard", self.token, str(os.getpid())),
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            handles = (W.HANDLE * 2)(self.ready, int(self.process._handle))
            # Two explicit refreshes can each consume the client's 30-second
            # budget; allow bounded journal/readback overhead and cancellation.
            deadline = time.monotonic() + 90
            while True:
                check_cancel()
                result = self.native.k.WaitForMultipleObjects(2, handles, False, 100)
                if result in (0, 1):
                    break
                if result != 258 or time.monotonic() >= deadline:
                    raise RuntimeError("等待自动热切换启动超时")
            check_cancel()
            report = self.status()
            if (report.get("phase") != "ready" or self.process.poll() is not None
                    or not _same_targets(_saved_targets(report.get("targets")), expected)):
                raise RuntimeError(report.get("error") or report.get("restore_error") or "输入恢复进程提前结束")
            current = _saved_targets(report.get("current_targets", report["targets"]))
            if not _same_root(expected, current):
                raise RuntimeError("重新连接的手柄已改变输入根节点")
            anchor = report.get("input_anchor")
            if anchor is not None:
                if not _same_targets(resolve_current_input(expected, anchor), current):
                    raise RuntimeError("当前手柄输入与恢复进程确认的实例不一致")
            elif not _same_targets(expected, current):
                raise RuntimeError("缺少物理连接核查，未改选手柄输入实例")
            self._current_connection = replace(self.connection, device_id=current.device_id)
        except Exception:
            try:
                self.stop()
            except Exception:
                pass  # Engine cleanup retries; retain the original start error.
            raise

    def status(self):
        report = _read(self.report_path) or {}
        if not isinstance(report, dict) or (report and report.get("token") != self.token):
            raise RuntimeError("热切换状态记录不属于当前会话")
        return report

    @property
    def error(self):
        if self._last_error is not None:
            return self._last_error
        if self.process is not None and self.process.poll() is not None:
            return RuntimeError("热切换恢复进程意外退出，正在停止转换并恢复普通输入")
        return None

    def stop(self):
        if self.stop_event:
            self.native.k.SetEvent(self.stop_event)
        finished = self.process is None
        try:
            if self.process is not None:
                self.process.wait(timeout=45)
                finished = True
                report = self.status()
                if not report.get("restored"):
                    record = _load_transaction(self.paths)
                    if record and record["token"] == self.token:
                        claim = None
                        try:
                            claim = self.native.claim()
                            recovered = _recover_locked(self.paths, self.native, HotSwitchClient(self.paths),
                                                        expected_token=self.token, recover_outputs=False)
                        finally:
                            self.native.release(claim)
                        report.update(recovered)
                        save_json(self.report_path, report)
                    if not report.get("restored"):
                        raise RuntimeError(report.get("restore_error") or report.get("refresh_error") or "热切换输入恢复尚未完成")
                self.process = None
                self._last_error = None
        except Exception as exc:
            self._last_error = exc
            raise
        finally:
            if finished:
                for handle in (self.ready, self.stop_event):
                    self.native.close(handle)
                self.ready = self.stop_event = None
