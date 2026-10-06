"""Controller loop with injected I/O and backend construction; no Windows imports."""
from datetime import datetime, timezone
import math
import os
import sys
import threading
import time
import uuid
from dsbridge.core.modes import PROFILES
from dsbridge.core.ports import BridgeCancelled, ControllerDisconnected

def scale_feedback(value, gain):
    if len(value) != 2 or not math.isfinite(float(gain)):
        raise ValueError("震动参数无效")
    return tuple(min(1.0, max(0.0, float(v) * gain)) if math.isfinite(float(v)) else 0.0 for v in value)

class Engine:
    """Owns one input slot and all feedback writes until a clean stop."""
    def __init__(self, xinput, events, backend_factory=None, *, diagnostics=None, profiles=None):
        self.profiles = PROFILES if profiles is None else profiles
        self.diagnostics = diagnostics
        self.xinput = xinput
        self.events = events
        self.backend_factory = backend_factory
        self.thread = None
        self.cancel = threading.Event()
        self.gain = 0.7

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, index, mode):
        if mode not in self.profiles:
            raise ValueError("此版本仅支持 PS5 DualSense 桥接模式")
        if self.running:
            raise RuntimeError("请先停止当前模式")
        if self.xinput.get_state(index) is None:
            raise RuntimeError("所选手柄未连接，请重新检测")
        profile = self.profiles[mode]
        if profile.unique_input and sum(self.xinput.get_state(i) is not None for i in getattr(self.xinput, "slots", range(4))) != 1:
            raise RuntimeError("此模式请只连接目标手柄，避免输入与震动来源不一致")
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run, args=(index, mode), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancel.set()

    def _make_backend(self, mode):
        if mode not in self.profiles:
            raise ValueError("不支持的桥接模式")
        if self.backend_factory is None:
            raise RuntimeError("桥接引擎需要显式提供后端工厂")
        return self.backend_factory(mode)

    def _run(self, index, mode):
        profile = self.profiles[mode]
        backend = None
        session_id = uuid.uuid4().hex
        started_at = datetime.now(timezone.utc).isoformat()
        output_peak = (0.0, 0.0)
        output_nonzero_writes = 0
        failure = None
        disconnected = False
        metadata = None

        def check_start():
            if self.cancel.is_set():
                raise BridgeCancelled()
            if self.xinput.get_state(index) is None:
                raise ControllerDisconnected("手柄连接已断开，正在停止本次桥接。重新连接后请检测并启动。")
            error = getattr(backend, "error", None) if backend else None
            if error:
                raise error if isinstance(error, Exception) else RuntimeError(str(error))

        def session_record(phase):
            # Do not store button/axis input, audio samples or microphone data.
            return {"session_id": session_id, "process_id": os.getpid(),
                    "started_at": started_at, "updated_at": datetime.now(timezone.utc).isoformat(),
                    "phase": phase, "mode": profile.label, "slot": index, "gain": self.gain,
                    "output_peak": output_peak, "output_nonzero_writes": output_nonzero_writes,
                    "ps5": metadata, "error": failure}

        # The receiver owns waveform output; never overwrite it with XInput rumble.
        owns_rumble = not profile.managed_output
        try:
            check_start()
            backend = self._make_backend(mode)
            start_cancellable = getattr(backend, "start_cancellable", None)
            if start_cancellable:
                start_cancellable(check_start)
            else:
                backend.start()
            check_start()
            self.events.put(("status", "已启动：" + profile.label))
            last_value = None
            last_raw = (0, 0)
            last_write = 0.0
            last_ui = 0.0
            while not self.cancel.is_set():
                now = time.monotonic()
                state = self.xinput.get_state(index)
                if state is None:
                    raise ControllerDisconnected("手柄连接已断开，正在停止本次桥接。重新连接后请检测并启动。")
                if backend:
                    error = getattr(backend, "error", None)
                    if error:
                        raise error if isinstance(error, Exception) else RuntimeError(str(error))
                    if profile.managed_output:
                        backend.set_gain(self.gain)
                    backend.update(state)
                    raw = backend.poll_feedback()
                    # None means no new event, not a game-authored stop command.
                    if raw is not None:
                        last_raw = raw
                    value = last_raw if profile.managed_output else scale_feedback(last_raw, self.gain)
                    if owns_rumble and (value != last_value or now - last_write >= 0.1):
                        if self.xinput.set_rumble(index, *value) is False:
                            raise ControllerDisconnected("手柄已断开，正在停止本次桥接。重新连接后请检测并启动。")
                        if any(value):
                            output_nonzero_writes += 1
                        output_peak = tuple(max(output_peak[lane], value[lane]) for lane in (0, 1))
                        last_value, last_write = value, now
                else:
                    value = (0, 0)
                if now - last_ui >= 0.1:
                    metadata = backend.status() if hasattr(backend, "status") else None
                    if profile.managed_output and metadata and metadata.get("output"):
                        output_peak = tuple(metadata["output"]["peak"][:2])
                        output_nonzero_writes = metadata["output"]["nonzero_writes"]
                    self.events.put(("live", {"state": state, "rumble": value,
                                               "ps5": metadata, "session": session_record("running")}))
                    last_ui = now
                self.cancel.wait(0.004)
        except BridgeCancelled:
            pass
        except ControllerDisconnected as exc:
            disconnected = True
            failure = str(exc)
            self.events.put(("disconnected", str(exc)))
        except Exception as exc:
            failure = str(exc)
            if self.diagnostics:
                try:
                    self.diagnostics.exception("controller_engine", *sys.exc_info())
                except Exception:
                    pass  # Diagnostic storage failure must never bypass cleanup.
            self.events.put(("error", str(exc)))
        finally:
            # Stop motors before any potentially slow virtual-device cleanup.
            if owns_rumble:
                try:
                    self.xinput.set_rumble(index, 0, 0)
                except Exception:
                    pass
            if backend:
                try:
                    if hasattr(backend, "status"):
                        metadata = backend.status()
                except Exception:
                    pass
                try:
                    backend.stop()
                    cleanup_error = getattr(backend, "last_cleanup_error", None)
                    if cleanup_error:
                        self.events.put(("log", "设备清理提示：" + cleanup_error))
                    if hasattr(backend, "status"):
                        metadata = backend.status()
                except Exception as exc:
                    cleanup_failure = "停止后的设备恢复未完成：" + str(exc)
                    failure = failure or cleanup_failure
                    self.events.put(("error", cleanup_failure))
            close_input = getattr(self.xinput, "close", None)
            if close_input:
                try:
                    close_input()
                except Exception as exc:
                    failure = failure or ("停止后的输入资源释放未完成：" + str(exc))
                    self.events.put(("error", "停止后的输入资源释放未完成：" + str(exc)))
            self.events.put(("session_end", session_record("disconnected" if disconnected else "failed" if failure else "stopped")))
            self.events.put(("stopped", None))
