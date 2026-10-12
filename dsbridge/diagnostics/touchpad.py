"""Real View -> virtual DualSense click audit; no synthetic input or feedback.

The CLI opt-in starts the production bridge at zero gain. Only the selected
physical View boolean and verified USB/IP Sony input booleans reach the audit
record. HID access is GENERIC_READ; this module never sends an output report.

Pinned report layout (indices include report ID):
https://github.com/hbashton/VIIPER/blob/v0.1.9-rc4.6.6/device/dualsense/input_scheduler.go#L1042-L1084
https://github.com/hbashton/VIIPER/blob/v0.1.9-rc4.6.6/device/dualsense/const.go#L136-L143
https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/obtaining-hid-reports
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import json
import os
import queue
import threading
import time

# Only an abnormal driver that never completes cancellation reaches this list.
# Keep its I/O memory alive until the diagnostic process exits; never free an
# outstanding buffer merely to hide a failed cleanup in the report.
_PENDING_READS = []


class AuditFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SonyButtons:
    touchpad_click: bool
    create: bool
    touch1_inactive: bool
    touch2_inactive: bool


def parse_sony_input(report: bytes) -> SonyButtons:
    """Parse only the four relevant flags of a complete native USB report."""
    if not isinstance(report, bytes) or len(report) != 64 or report[0] != 1:
        raise AuditFailure("invalid_report", "虚拟 Sony 输入报告的长度或编号不符。")
    return SonyButtons(bool(report[10] & 2), bool(report[9] & 0x10),
                       bool(report[33] & 0x80), bool(report[37] & 0x80))


class _Edges:
    def __init__(self):
        self.pressed = False
        self.edges = []
        self.holds = 0
        self.released_at = None

    def observe(self, value, at, minimum_hold, minimum_release, target):
        if type(value) is not bool:
            raise ValueError("The observed button must be boolean")
        if self.edges and at < self.edges[-1][1]:
            raise ValueError("Button observations must be ordered")
        if value == self.pressed:
            return
        if value and self.released_at is not None and at - self.released_at < minimum_release:
            raise AuditFailure("short_release", "两次按下之间未保持松开，请每次松开约一秒。")
        if not value:
            if at - self.edges[-1][1] < minimum_hold:
                raise AuditFailure("short_hold", "按键未保持足够时间，请按住约一秒。")
            self.holds += 1
            self.released_at = at
        self.pressed = value
        self.edges.append((value, at))
        if len(self.edges) > target * 2:
            raise AuditFailure("extra_edges", "检测到超过三次的按键周期，请重新测试。")

    def snapshot(self, now, minimum_hold):
        return {"pressed": self.pressed,
                "presses": sum(value for value, _ in self.edges),
                "releases": sum(not value for value, _ in self.edges),
                "completed_holds": self.holds,
                "holding_long_enough": bool(self.pressed and self.edges
                                             and now - self.edges[-1][1] >= minimum_hold)}


class TouchpadAudit:
    """Pure edge/hold state machine; observations contain no full input or ID."""
    def __init__(self, *, target=3, minimum_hold=.65, minimum_release=.65, maximum_lag=.5):
        self.target = target
        self.minimum_hold = minimum_hold
        self.minimum_release = minimum_release
        self.maximum_lag = maximum_lag
        self.physical = _Edges()
        self.sony = _Edges()
        self.reports = 0
        self.create_seen = False
        self.contacts_inactive = True
        self.matched_edges = 0

    def observe_physical(self, view, at):
        self.physical.observe(view, at, self.minimum_hold, self.minimum_release, self.target)

    def observe_sony(self, buttons, at):
        self.reports += 1
        self.create_seen |= buttons.create
        self.contacts_inactive &= buttons.touch1_inactive and buttons.touch2_inactive
        if self.create_seen:
            raise AuditFailure("create_pressed", "虚拟 Sony 同时触发了 Create，映射验证失败。")
        if not self.contacts_inactive:
            raise AuditFailure("touch_active", "虚拟 Sony 出现触点激活，点击映射验证失败。")
        self.sony.observe(buttons.touchpad_click, at, self.minimum_hold,
                          self.minimum_release, self.target)

    def check(self, now):
        pairs = min(len(self.physical.edges), len(self.sony.edges))
        for index in range(self.matched_edges, pairs):
            view, physical_at = self.physical.edges[index]
            touchpad, sony_at = self.sony.edges[index]
            if view != touchpad or not -.05 <= sony_at - physical_at <= self.maximum_lag:
                raise AuditFailure("edge_mismatch", "实体 View 与虚拟触摸板的边沿未对应。")
        self.matched_edges = pairs
        for edges in (self.physical.edges, self.sony.edges):
            if len(edges) > pairs and now - edges[pairs][1] > self.maximum_lag:
                raise AuditFailure("missing_edge", "实体 View 与虚拟触摸板缺少对应的按下或松开。")
        complete = pairs == self.target * 2
        released = not self.physical.pressed and not self.sony.pressed
        settled = (complete and released
                   and now - max(self.physical.released_at, self.sony.released_at) >= self.minimum_release)
        return bool(settled and self.physical.holds == self.target and self.sony.holds == self.target)

    def snapshot(self, now):
        return {"physical_view": self.physical.snapshot(now, self.minimum_hold),
                "virtual_touchpad": self.sony.snapshot(now, self.minimum_hold),
                "sony_reports": self.reports, "matched_edges": self.matched_edges,
                "create_seen": self.create_seen, "contacts_always_inactive": self.contacts_inactive}


class _ObservedInput:
    """Transparent production input proxy; inspect View on every engine poll."""
    def __init__(self, source, slot):
        self.source, self.slot = source, slot
        self.samples = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.armed = False
        self.latest = None
        self.previous = False
        self.nonzero_attempts = 0

    @property
    def slots(self):
        return self.source.slots

    def get_state(self, slot):
        state = self.source.get_state(slot)
        if slot == self.slot:
            value = None if state is None else bool(state["buttons"] & 0x20)
            with self.lock:
                self.latest = value
                if self.armed and value != self.previous:
                    self.samples.put((time.monotonic(), value))
                    self.previous = value
        return state

    def arm(self):
        with self.lock:
            if self.latest is not False:
                return False
            self.previous = False
            self.armed = True
            return True

    def prepare_input(self, slot):
        return self.source.prepare_input(slot)

    def validate_input(self, slot):
        validate = getattr(self.source, "validate_input", None)
        return validate(slot) if validate else True

    def set_rumble(self, slot, left, right):
        if left != 0 or right != 0:
            self.nonzero_attempts += 1
            raise AuditFailure("nonzero_output", "零强度诊断拒绝了非零震动输出。")
        return self.source.set_rumble(slot, 0, 0)

    def close(self):
        self.source.close()


class _Offset(C.Structure):
    _fields_ = [("Offset", C.c_uint32), ("OffsetHigh", C.c_uint32)]


class _OverlappedUnion(C.Union):
    _fields_ = [("offset", _Offset), ("Pointer", C.c_void_p)]


class _Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("position", _OverlappedUnion), ("hEvent", C.c_void_p)]


class _HidReader:
    """One outstanding overlapped read; cancel and reap before freeing memory."""
    def __init__(self, path):
        from dsbridge.diagnostics.sony_writer import _Api, _matching, describe_dualsense_outputs
        matches = [row for row in describe_dualsense_outputs() if row["path"].casefold() == path.casefold()]
        if len(matches) != 1:
            raise AuditFailure("hid_identity", "无法唯一确认本次 USB/IP 虚拟 Sony 输入接口。")
        self.api = _Api()
        self.handle = self.event = None
        self.pending = False
        self.buffer = C.create_string_buffer(64)
        self.overlapped = _Overlapped()
        kernel = self.api.kernel
        specs = (("ReadFile", [C.c_void_p, C.c_void_p, C.c_uint32,
                               C.POINTER(C.c_uint32), C.POINTER(_Overlapped)], C.c_int),
                 ("CreateEventW", [C.c_void_p, C.c_int, C.c_int, C.c_wchar_p], C.c_void_p),
                 ("ResetEvent", [C.c_void_p], C.c_int),
                 ("WaitForSingleObject", [C.c_void_p, C.c_uint32], C.c_uint32),
                 ("CancelIoEx", [C.c_void_p, C.POINTER(_Overlapped)], C.c_int),
                 ("GetOverlappedResult", [C.c_void_p, C.POINTER(_Overlapped),
                                          C.POINTER(C.c_uint32), C.c_int], C.c_int))
        for name, args, result in specs:
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result
        try:
            self.handle = kernel.CreateFileW(path, 0x80000000, 3, None, 3, 0x40000000, None)
            if self.handle in (None, C.c_void_p(-1).value):
                self.handle = None
                raise AuditFailure("hid_open", "无法只读打开虚拟 Sony 输入接口。")
            attrs, caps = self.api.inspect(self.handle)
            if not _matching(attrs, caps) or caps.InputReportByteLength != 64:
                raise AuditFailure("hid_caps", "虚拟 Sony 输入接口在打开时发生变化。")
            self.event = kernel.CreateEventW(None, True, False, None)
            if not self.event:
                raise AuditFailure("hid_event", "无法建立虚拟 Sony 读取事件。")
        except Exception:
            self.close()
            raise

    def _completed(self, wait=False):
        count = C.c_uint32()
        ok = self.api.kernel.GetOverlappedResult(self.handle, C.byref(self.overlapped),
                                                C.byref(count), wait)
        error = C.get_last_error() if not ok else 0
        self.pending = False
        if not ok:
            if error == 995:  # ERROR_OPERATION_ABORTED after our cancellation.
                return None
            raise AuditFailure("hid_read", "虚拟 Sony 输入读取失败或设备已断开。")
        if count.value != 64:
            raise AuditFailure("hid_short_read", "虚拟 Sony 输入报告不完整。")
        return self.buffer.raw

    def _cancel(self):
        if self.pending:
            cancel_error = 0
            if self.handle:
                cancelled = self.api.kernel.CancelIoEx(self.handle, C.byref(self.overlapped))
                cancel_error = C.get_last_error() if not cancelled else 0
            # The buffer, OVERLAPPED and event stay alive until the request ends.
            outcome = self.api.kernel.WaitForSingleObject(self.event, 2000)
            if outcome != 0:
                raise AuditFailure("hid_cancel_timeout", "虚拟 Sony 输入读取取消未及时完成。")
            if not self.handle:
                self.pending = False
                return None  # Closed handle, but event proves I/O memory is safe.
            result = self._completed()
            if cancel_error not in (0, 1168):  # Completion can race cancellation.
                raise AuditFailure("hid_cancel", "无法取消虚拟 Sony 输入读取。")
            return result
        return None

    def read(self, timeout=.1):
        if not self.handle or self.pending:
            raise RuntimeError("HID reader is closed or already has a pending read")
        if not self.api.kernel.ResetEvent(self.event):
            raise AuditFailure("hid_event", "无法重置虚拟 Sony 读取事件。")
        self.overlapped = _Overlapped()
        self.overlapped.hEvent = self.event
        # Async handles obtain the length through GetOverlappedResult, including
        # immediate completion; lpNumberOfBytesRead must be NULL for this use.
        self.pending = True
        ok = self.api.kernel.ReadFile(self.handle, self.buffer, 64, None, C.byref(self.overlapped))
        if ok:
            return self._completed()
        if C.get_last_error() != 997:  # ERROR_IO_PENDING
            self.pending = False
            raise AuditFailure("hid_read", "虚拟 Sony 输入读取失败或设备已断开。")
        outcome = self.api.kernel.WaitForSingleObject(self.event, max(1, int(timeout * 1000)))
        if outcome == 0:
            return self._completed()
        if outcome == 258:  # WAIT_TIMEOUT; do not leave a request pending.
            return self._cancel()
        self._cancel()
        raise AuditFailure("hid_wait", "虚拟 Sony 输入读取等待失败。")

    def close(self):
        errors = []
        try:
            self._cancel()
        except Exception:
            errors.append("cancel")
        finally:
            if self.handle:
                if self.api.kernel.CloseHandle(self.handle):
                    self.handle = None
                else:
                    errors.append("handle")
            if self.pending and self.handle is None:
                # Closing this reader's own handle also cancels its reads. Do
                # not release the event/buffer before that completion arrives.
                if self.api.kernel.WaitForSingleObject(self.event, 2000) == 0:
                    self.pending = False
            if self.pending:
                if not any(item is self for item in _PENDING_READS):
                    _PENDING_READS.append(self)
                errors.append("unreaped")
            elif self.event:
                if self.api.kernel.CloseHandle(self.event):
                    self.event = None
                else:
                    errors.append("event")
            if not self.pending:
                _PENDING_READS[:] = [item for item in _PENDING_READS if item is not self]
        if errors:
            raise AuditFailure("hid_cleanup", "虚拟 Sony 读取请求或句柄释放未完全确认。")


def _save(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(context, *, timeout=105):
    """Explicit CLI entry; only the human can generate the three button cycles."""
    from dsbridge.core.engine import Engine
    from dsbridge.diagnostics.sony_writer import enum_dualsense_outputs
    if not 90 <= timeout <= 120:
        raise ValueError("Touchpad audit duration must be 90..120 seconds")
    path = context.paths.state / "touchpad-audit.json"
    started = time.monotonic()
    deadline = started + timeout
    report = {"version": 1, "phase": "starting", "passed": False,
              "timeout_seconds": timeout, "zero_gain": True,
              "synthetic_input": False, "synthetic_feedback": False,
              "engine_stopped": False, "hid_closed": True, "input_closed": True}
    _save(path, report)
    engine = reader = source = observed = None
    backend_holder = []
    events = queue.Queue()
    audit = TouchpadAudit()
    engine_started = False
    cycles_complete = False
    failure = None
    cleanup_errors = []

    def check_events(require_running=True):
        nonlocal engine_started
        while True:
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == "status" and str(value).startswith("已启动"):
                engine_started = True
            if kind in ("error", "disconnected"):
                raise AuditFailure("engine_error", "生产桥接报告异常或实体手柄断开，测试已停止。")
            if kind == "session_end" and value.get("error"):
                raise AuditFailure("engine_cleanup", "生产桥接报告设备恢复异常。")
        if require_running and engine and engine.thread and not engine.running:
            raise AuditFailure("engine_stopped", "生产桥接提前停止，测试失败。")
        if backend_holder and getattr(backend_holder[0], "error", None):
            raise AuditFailure("backend_error", "生产桥接后端发生异常，测试已停止。")

    def drain_physical():
        while True:
            try:
                at, value = observed.samples.get_nowait()
            except queue.Empty:
                break
            if value is None:
                raise AuditFailure("physical_disconnected", "实体手柄在测试中断开。")
            audit.observe_physical(value, at)

    def save_progress(phase):
        report.update(audit.snapshot(time.monotonic()))
        report["phase"] = phase
        _save(path, report)

    try:
        # Detect the unique physical source before any virtual interface exists.
        probe = context.create_input(1)
        try:
            slots = [slot for slot in probe.slots if probe.get_state(slot) is not None]
        finally:
            probe.close()  # Release the main thread's preflight COM apartment.
        if len(slots) != 1:
            raise AuditFailure("physical_count", "测试需要唯一且已唤醒的实体手柄。")
        slot = slots[0]
        connection = context.detect_connection(1, slot)
        if connection.model != "flydigi.apex6pro":
            raise AuditFailure("physical_model", "当前连接未确认为八爪鱼 6 Pro，请先检测或绑定型号。")
        mode = connection.profile
        if mode not in context.registry.profiles:
            raise AuditFailure("physical_profile", "当前连接没有可用的生产桥接模式。")
        source = context.create_input(mode)
        observed = _ObservedInput(source, slot)
        report["input_closed"] = False

        def factory(selected_mode):
            backend = context.create_backend(selected_mode)
            backend_holder.append(backend)
            # Managed output is armed during startup, before Engine's first loop.
            if hasattr(backend, "set_gain"):
                backend.set_gain(0)
            return backend

        engine = Engine(observed, events, factory, diagnostics=context.diagnostics,
                        profiles=context.registry.profiles)
        engine.gain = 0
        engine.start(slot, mode)
        startup_deadline = min(deadline, time.monotonic() + 30)
        while not engine_started:
            check_events()
            if time.monotonic() >= startup_deadline:
                raise AuditFailure("startup_timeout", "生产桥接启动超时。")
            time.sleep(.02)
        hid_deadline = min(deadline, time.monotonic() + 12)
        while True:
            check_events()
            candidates = enum_dualsense_outputs()
            if len(candidates) > 1:
                raise AuditFailure("hid_count", "检测到多个虚拟 Sony 接口，无法确定验证对象。")
            if len(candidates) == 1:
                reader = _HidReader(candidates[0])
                report["hid_closed"] = False
                break
            if time.monotonic() >= hid_deadline:
                raise AuditFailure("hid_missing", "没有找到本次唯一的 USB/IP 虚拟 Sony 输入接口。")
            time.sleep(.05)
        # Establish a fresh neutral report before exposing the ready prompt.
        neutral_at = None
        neutral_deadline = min(deadline, time.monotonic() + 15)
        report["phase"] = "waiting_release"
        report["instruction"] = "请先松开圈出的 View 小按键，等待准备提示。"
        _save(path, report)
        while True:
            check_events()
            raw = reader.read(.1)
            now = time.monotonic()
            if raw is not None:
                buttons = parse_sony_input(raw)
                if buttons.create or not (buttons.touch1_inactive and buttons.touch2_inactive):
                    raise AuditFailure("invalid_baseline", "准备期间虚拟 Sony 的 Create 或触点状态不符合预期。")
                with observed.lock:
                    neutral = observed.latest is False and not buttons.touchpad_click
                neutral_at = (neutral_at if neutral_at is not None else now) if neutral else None
                if neutral_at is not None and now - neutral_at >= .25 and observed.arm():
                    break
            if now >= neutral_deadline:
                raise AuditFailure("baseline_timeout", "未能确认实体和虚拟按键已松开。")
        report["instruction"] = "准备完成：按住圈出的 View 小按键约一秒，松开约一秒，重复三次。"
        save_progress("ready")
        print(report["instruction"], flush=True)
        last_save = time.monotonic()
        last_report = last_save
        while time.monotonic() < deadline:
            check_events()
            drain_physical()
            raw = reader.read(.1)
            now = time.monotonic()
            drain_physical()
            if raw is not None:
                audit.observe_sony(parse_sony_input(raw), now)
                last_report = now
            if now - last_report > 2:
                raise AuditFailure("hid_no_reports", "虚拟 Sony 输入报告中断，测试失败。")
            if audit.check(now):
                cycles_complete = True
                break
            if now - last_save >= .2:
                save_progress("ready")
                last_save = now
        else:
            raise AuditFailure("audit_timeout", "测试超时：三次按下、保持和松开尚未全部对应。")
        save_progress("stopping")
    except Exception as exc:
        failure = exc
        report["error_code"] = getattr(exc, "code", "runtime_error")
        # Native exceptions can contain device paths. Keep identifiers out of JSON.
        report["error"] = str(exc) if isinstance(exc, AuditFailure) else "诊断发生异常，测试失败。"
    except (KeyboardInterrupt, SystemExit) as exc:
        failure = exc
        report["error_code"] = "interrupted"
        report["error"] = "测试被中断，三次完整按键周期未确认。"
        raise
    finally:
        if engine:
            engine.stop()
        if reader:
            try:
                reader.close()
            except Exception:
                cleanup_errors.append("hid_cleanup")
            report["hid_closed"] = reader.handle is None and reader.event is None
        if engine and engine.thread:
            engine.thread.join(35)
            if engine.running:
                cleanup_errors.append("engine_join")
                # Stop only the backend created by this diagnostic session.
                if backend_holder:
                    try:
                        backend_holder[0].stop()
                    except Exception:
                        cleanup_errors.append("backend_cleanup")
                engine.thread.join(5)
        if reader and (reader.handle or reader.event or reader.pending):
            # Virtual removal can complete an abnormal pending read; make one
            # bounded retry after the production engine has finished cleanup.
            try:
                reader.close()
            except Exception:
                if "hid_cleanup" not in cleanup_errors:
                    cleanup_errors.append("hid_cleanup")
            report["hid_closed"] = reader.handle is None and reader.event is None and not reader.pending
        report["engine_stopped"] = not engine or not engine.running
        try:
            check_events(require_running=False)
        except Exception as exc:
            failure = failure or exc
        if source:
            try:
                source.close()  # Main-thread COM is separate from Engine's COM.
                report["input_closed"] = True
            except Exception:
                cleanup_errors.append("input_cleanup")
        if backend_holder and getattr(backend_holder[0], "last_cleanup_error", None):
            cleanup_errors.append("backend_cleanup")
        output_zero = True
        if backend_holder:
            try:
                status = backend_holder[0].status()
                receiver = status.get("receiver")
                if receiver is not None:
                    counts = receiver["motor_nonzero_writes"]
                    if len(counts) != 4 or any(type(value) is not int or value < 0 for value in counts):
                        raise ValueError("Invalid receiver output counters")
                    report["receiver_motor_nonzero_writes"] = list(counts)
                    output_zero &= not any(counts)
                output = status.get("output")
                if output is not None:
                    count = output["nonzero_writes"]
                    if type(count) is not int or count < 0:
                        raise ValueError("Invalid output counter")
                    report["output_nonzero_writes"] = count
                    output_zero &= count == 0
                if mode == 5 and receiver is None:
                    raise ValueError("Missing receiver output counters")
            except Exception:
                output_zero = False
                cleanup_errors.append("output_counters")
        report["physical_output_confirmed_zero"] = output_zero
        report["nonzero_output_attempts"] = observed.nonzero_attempts if observed else 0
        report["cleanup_errors"] = cleanup_errors
        report["three_cycles_complete"] = cycles_complete
        report["passed"] = bool(cycles_complete and output_zero and not failure and not cleanup_errors and report["engine_stopped"]
                                and report["hid_closed"] and report["input_closed"]
                                and not report["nonzero_output_attempts"])
        if failure and "error_code" not in report:
            report["error_code"] = getattr(failure, "code", "cleanup_error")
            report["error"] = str(failure) if isinstance(failure, AuditFailure) else "诊断资源恢复失败。"
        save_progress("passed" if report["passed"] else "failed")
    if not report["passed"]:
        raise RuntimeError("触摸板点击实机验证未通过，详见 touchpad-audit.json。") from failure
    print("三次实体 View 与虚拟触摸板的按下、保持和松开全部通过，桥接已停止。", flush=True)
    return report
