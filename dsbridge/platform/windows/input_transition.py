"""Coordinate selected-input hot switching before a virtual DS is attached.

This Windows layer owns no motor output and does not interpret controller state.
The engine supplies a cancellation-only callback during planned PnP removal;
checking the old slot for disconnection here would abort every valid switch.
An input port's rebind(connection) must return a uniquely verified slot for the
same physical instance, or None while that instance cannot be bound safely.
"""
from __future__ import annotations

from dataclasses import replace
import os
import time

from dsbridge.core.connection import ControllerConnection
from dsbridge.core.ports import BridgeCancelled
from dsbridge.platform.windows.controller_usage import probe_controller_usage
from dsbridge.platform.windows.hot_switch_lease import HotSwitchLease
from dsbridge.platform.windows.input_transition_targets import resolve_input_targets
from dsbridge.runtime.paths import as_paths

_REBIND_SECONDS = 12.0
_POLL_SECONDS = 0.05
_LEASE_STATUS_FIELDS = ("phase", "restored", "input_visibility_restored", "refresh_pending", "guard_pid")


def _usage_status(report):
    """Keep only process-handle evidence; never retain input or device paths."""
    if not isinstance(report, dict):
        return {"state": "unknown", "clients": [], "complete": False}
    clients = []
    for row in report.get("clients", ()):
        if not isinstance(row, dict) or type(row.get("pid")) is not int or row["pid"] <= 4:
            continue
        client = {name: row[name] for name in ("pid", "name", "source", "evidence") if name in row}
        clients.append(client)
    # The concrete probe never proves that every input API has no consumers.
    # A hypothetical `none` response must not skip the conservative refresh.
    return {"state": "held" if clients else "unknown", "clients": clients, "complete": False,
            "handle_scan_complete": report.get("handle_scan_complete") is True,
            "scanned_handles": report.get("scanned_handles", 0)}


class InputTransition:
    """One transition and its restore guard; a new instance is used per start."""
    def __init__(self, paths, input_port, index, mode, events, *, ensure_service):
        self.paths = as_paths(paths)
        self.input_port = input_port
        self.index = index
        self.mode = mode
        self.events = events
        self.ensure_service = ensure_service
        self.slot = index
        self.current_connection = None
        self._lease = None
        self._lease_status = {}
        self._usage = _usage_status(None)
        self._phase = "new"
        self._last_error = None
        self._attempted = False
        self._stopped = False
        self._refresh_requested = False

    def _notify(self, message, *, log=False):
        # A failed observer must not prevent the guard from restoring input.
        try:
            self.events.put(("log" if log else "status", str(message)))
        except Exception:
            pass

    @property
    def error(self):
        if self._last_error is not None:
            return self._last_error
        if self._lease is not None and not self._stopped:
            return self._lease.error
        return None

    def _check(self, check_cancel):
        check_cancel()
        failure = self._lease.error if self._lease is not None else None
        if failure:
            raise failure if isinstance(failure, Exception) else RuntimeError(str(failure))

    def start_cancellable(self, check_cancel):
        if self._attempted or self._stopped:
            raise RuntimeError("此输入切换会话已经使用，请重新启动转换。")
        if type(self.mode) is not int or self.mode not in (1, 5):
            raise ValueError("自动输入切换仅用于内置 DS 桥接模式。")
        if (type(self.index) is not int or self.index not in getattr(self.input_port, "slots", range(4))
                or not callable(getattr(self.input_port, "connection", None))
                or not callable(getattr(self.input_port, "rebind", None))):
            raise RuntimeError("当前输入接口不能安全定位热切换后的手柄。")
        self._attempted = True
        try:
            check_cancel()
            self._phase = "checking"
            self._notify("正在检查所选手柄的输入连接…")
            connection = self.input_port.connection(self.index)
            # Reject unknown/shared layouts before installing a service,
            # creating a guard, changing visibility, or restarting anything.
            targets = resolve_input_targets(connection)
            check_cancel()
            try:
                report = probe_controller_usage(connection, exclude_pids=(os.getpid(),))
            except BridgeCancelled:
                raise
            except Exception:
                # Usage evidence is advisory. The exact restart scope was
                # already verified; an inaccessible probe cannot prove idle.
                report = None
            self._usage = _usage_status(report)
            if self._usage["state"] == "held":
                self._notify("检测到已有程序持有所选手柄的输入连接，将自动刷新连接。", log=True)
            else:
                self._notify("无法完整确认现有输入连接；启动时将自动刷新所选手柄。", log=True)
            check_cancel()
            self._phase = "service"
            self.ensure_service(self.paths, self._notify, check_cancel)
            check_cancel()
            self._lease = HotSwitchLease(self.paths, connection, targets=targets)
            self._phase = "switching"
            self._refresh_requested = True
            self._notify("正在自动切换手柄输入…")
            self._lease.start_cancellable(check_cancel)
            self._check(check_cancel)
            # Only the guard's independently verified physical-root migration
            # may change the HID instance after a USB port cycle. Never infer a
            # replacement by enumerating the same VID/PID in this coordinator.
            current = self._lease.current_connection
            if (not isinstance(current, ControllerConnection) or not current.device_id
                    or replace(current, device_id=connection.device_id) != connection):
                raise RuntimeError("输入恢复进程未提供已验证的当前手柄连接，已取消启动。")
            self.current_connection = current
            self._phase = "reconnecting"
            self._notify("正在等待同一只手柄重新连接…")
            deadline = time.monotonic() + _REBIND_SECONDS
            while True:
                self._check(check_cancel)
                rebound = self.input_port.rebind(current)
                self._check(check_cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("热切换后未能重新定位同一只手柄，已取消启动并尝试恢复普通输入。")
                if rebound is not None:
                    if type(rebound) is not int or rebound not in getattr(self.input_port, "slots", range(4)):
                        raise RuntimeError("输入接口返回了无效的手柄槽位，转换已取消。")
                    self.slot = rebound
                    self._phase = "ready"
                    self._notify("手柄输入已切换，正在连接虚拟 DS…")
                    return
                time.sleep(min(_POLL_SECONDS, remaining))
        except Exception as exc:
            self._last_error = exc
            try:
                self.stop()
            except Exception as cleanup:
                self._notify("热切换输入恢复尚未完成：" + str(cleanup), log=True)
            self._phase = "cancelled" if isinstance(exc, BridgeCancelled) else "failed"
            raise

    def _read_lease_status(self):
        if self._lease is None:
            return dict(self._lease_status)
        try:
            report = self._lease.status()
            self._lease_status = {key: report[key] for key in _LEASE_STATUS_FIELDS if key in report}
        except Exception:
            return {**self._lease_status, "status_unavailable": True}
        return dict(self._lease_status)

    def status(self):
        return {"phase": self._phase, "slot": self.slot, "refresh_requested": self._refresh_requested,
                "usage": {**self._usage, "clients": [dict(value) for value in self._usage["clients"]]},
                "lease": self._read_lease_status(),
                "error": str(self._last_error) if self._last_error else None}

    def stop(self):
        if self._stopped:
            return
        self._phase = "restoring"
        try:
            if self._lease is not None:
                self._lease.stop()
                self._read_lease_status()
            self._stopped = True
            self._phase = "stopped"
        except Exception as exc:
            self._last_error = exc
            self._phase = "restore_failed"
            # Keep the lease and do not mark stopped, so engine cleanup can
            # retry restoration and report a remaining guard/service failure.
            raise
