"""APEX 6 physical output only: bounded DSP, worker and motor restoration."""
from collections import deque
import math
import threading
import time
from dsbridge.core.feedback import SonyFeedback
from dsbridge.controllers.flydigi.apex6.hid import ReceiverError
from dsbridge.controllers.flydigi.apex6.session import Apex6Session, ZERO
from dsbridge.controllers.flydigi.apex6.monitor import ReceiverMonitor
from dsbridge.controllers.flydigi.apex6.dsp import StereoDecimator, TriggerRenderer, TriggerRouter


class Apex6Output:
    def __init__(self, base, config, *, session_factory=Apex6Session, monitor_factory=ReceiverMonitor):
        self.session = session_factory(base)
        self.monitor_factory, self.monitor = monitor_factory, None
        self.native_gain = float(config.get("haptics_gain", 1.0))
        self.gain = 0.7
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._armed = threading.Event()
        self._thread = None
        self._failure = None
        self.last_cleanup_error = None
        self._dsp = StereoDecimator()
        self._samples = deque()
        self._native_at = 0.
        self._primed = False
        self._rumble = (0., 0.)
        self._rumble_phase = [0., 0.]
        self._positions = (0, 0)
        self._triggers = (TriggerRenderer(), TriggerRenderer())
        self._router = TriggerRouter()
        self._trigger_updates = [0, 0]
        self._trigger_nonzero = [0, 0]
        self._trigger_modes = [0, 0]
        self._trigger_data = ["", ""]
        self._unsupported = 0
        self._trigger_error = None
        self._last_output = (0.,) * 4
        self._output_peak = (0.,) * 4
        self._nonzero_writes = [0] * 4
        self._packets = self._underruns = self._discarded = self._late = 0
        self._nonzero_packets = 0
        self._pcm_frames = 0
        self._cleanup_status = "尚未启动"

    def start(self):
        return self.start_cancellable(lambda: None)

    def start_cancellable(self, check):
        self._cancel.clear()
        self._ready.clear()
        self._armed.clear()
        self._thread = threading.Thread(target=self._worker, name="Receiver haptic waveforms", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        try:
            while not self._ready.wait(0.05):
                check()
                if time.monotonic() >= deadline:
                    raise ReceiverError("接收器启动超时，正在停止并恢复")
            check()
            if self._failure:
                raise self._failure
        except Exception:
            self._cancel.set()
            raise

    def arm(self):
        self._cleanup_status = "运行中"
        self._armed.set()

    @property
    def error(self):
        return self._failure or (self.monitor.error if self.monitor else None)

    def set_gain(self, gain):
        if not math.isfinite(gain) or not 0 <= gain <= 1.5:
            raise ValueError("接收器震动强度无效")
        with self._lock:
            self.gain = gain

    def update(self, state):
        with self._lock:
            self._positions = (state["left_trigger"], state["right_trigger"])

    def accept(self, feedback: SonyFeedback):
        # Event handling only enqueues. HID I/O belongs to the worker.
        with self._lock:
            if feedback.rumble is not None:
                self._rumble = feedback.rumble
            for update in feedback.triggers:
                lane = update.lane
                self._trigger_updates[lane] += 1
                self._trigger_modes[lane] = update.mode
                self._trigger_data[lane] = update.raw.hex()
                if update.error:
                    self._unsupported += 1
                    self._trigger_error = update.error
                if update.effect.kind != "off":
                    self._trigger_nonzero[lane] += 1
                self._triggers[lane].set_effect(update.effect)
            if feedback.pcm is None:
                return
            if feedback.sample_rate != 3000:
                raise ValueError("APEX 6 转换器需要 3 kHz Sony 触觉输入")
            at = feedback.at
            if at - self._native_at > 0.05 or len(self._samples) + 11 > 40:
                self._discarded += len(self._samples)
                self._samples.clear()
                self._dsp.reset()
                self._primed = False
            self._samples.extend((sample, at) for sample in self._dsp.feed(*feedback.pcm))
            self._native_at = at
            self._pcm_frames += 1

    def _render(self, now):
        with self._lock:
            if self._samples and now - self._samples[0][1] > 0.04:
                self._discarded += len(self._samples)
                self._samples.clear()
                self._dsp.reset()
                self._primed = False
            if not self._primed and len(self._samples) >= 16:
                self._primed = True
            if self._primed and len(self._samples) >= 8:
                native = tuple(self._samples.popleft()[0] for _ in range(8))
            else:
                native = ((0., 0.),) * 8
                if self._primed:
                    self._underruns += 1
                self._primed = False
            gain = self.gain
            grips = [[], []]
            for i in range(8):
                for lane, frequency in ((0, 80), (1, 160)):
                    self._rumble_phase[lane] = (self._rumble_phase[lane] + frequency / 1000) % 1
                    fallback = 44 * self._rumble[lane] * math.sin(2 * math.pi * self._rumble_phase[lane])
                    value = (native[i][lane] * self.native_gain + fallback) * gain
                    grips[lane].append(max(-127, min(127, round(value))))
            triggers = tuple(renderer.render(self._positions[lane], gain) for lane, renderer in enumerate(self._triggers))
            selector, trigger, enabled = self._router.choose(*triggers)
            return tuple(grips[0]), tuple(grips[1]), trigger, selector, enabled, triggers

    def _worker(self):
        try:
            # Claim, HID handle, waveform writes and restoration have one owner.
            self.session.cancel = self._cancel
            self.session.start()
            if self.monitor_factory:
                self.monitor = self.monitor_factory(self.session)
                self.monitor.start()
            self._ready.set()
            while not self._armed.is_set():
                if self._cancel.wait(0.01):
                    return
                if self.monitor and self.monitor.error:
                    raise self.monitor.error
            deadline = time.perf_counter()
            while not self._cancel.is_set():
                now = time.perf_counter()
                if self.monitor and self.monitor.error:
                    raise self.monitor.error
                left, right, trigger, selector, enabled, rendered_triggers = self._render(time.monotonic())
                self.session.send(left, right, trigger, selector=selector, trigger_enabled=True)
                # Use zero Both when inactive to clear either previous trigger.
                lanes = (left, right, trigger if selector in (0, 2) else ZERO,
                         trigger if selector in (1, 2) else ZERO)
                output = tuple(max(abs(v) for v in lane) / 127 for lane in lanes)
                with self._lock:
                    self._last_output = output
                    self._output_peak = tuple(max(a, b) for a, b in zip(self._output_peak, output))
                    for lane, value in enumerate(output):
                        self._nonzero_writes[lane] += bool(value)
                    self._packets += 1
                    self._nonzero_packets += bool(any(output))
                deadline += 0.008
                if time.perf_counter() - deadline > 0.016:
                    # Never issue a burst to catch up after slow queries/I/O.
                    with self._lock:
                        self._late += 1
                    deadline = time.perf_counter() + 0.008
                self._cancel.wait(max(0., deadline - time.perf_counter()))
        except Exception as exc:
            self._failure = exc
            self._cancel.set()
        finally:
            cleanup_errors = []
            # A failed neutral write must not skip joining the monitor or
            # restoring the volatile motor routing on the owning thread.
            actions = []
            if self.session.transport:
                actions.append(self.session.neutral)
            if self.monitor:
                actions.append(self.monitor.stop)
            actions.append(self.session.stop)
            for action in actions:
                try:
                    action()
                except Exception as exc:
                    cleanup_errors.append(str(exc))
                    if not self._failure:
                        self._failure = exc
            cleanup_errors.extend(self.session.cleanup_errors)
            self._cleanup_status = "已恢复原配置" if self.session.restored else "未修改配置" if not self.session.journal else "恢复待处理"
            if cleanup_errors:
                self.last_cleanup_error = "；".join(cleanup_errors)
            self._ready.set()

    def poll_feedback(self):
        if self.error:
            raise self.error
        with self._lock:
            return self._last_output[:2]  # UI meter only; never sent to XInput.

    def status(self):
        result = {}
        with self._lock:
            receiver = {"identity": self.session.identity, "connected": self._thread is not None and self._thread.is_alive(),
                        "packets": self._packets, "nonzero_packets": self._nonzero_packets,
                        "motor_nonzero_writes": tuple(self._nonzero_writes),
                        "motor_peak": self._output_peak, "motor_output": self._last_output,
                        "pcm_frames": self._pcm_frames, "queued_samples": len(self._samples),
                        "underruns": self._underruns, "discarded_samples": self._discarded,
                        "late_ticks": self._late, "trigger_reports": tuple(self._trigger_updates),
                        "identity_checks": self.monitor.checks if self.monitor else 0,
                        "trigger_nonzero_effects": tuple(self._trigger_nonzero),
                        "trigger_modes": tuple(self._trigger_modes), "trigger_effect_bytes": tuple(self._trigger_data),
                        "unsupported_trigger_reports": self._unsupported, "trigger_error": self._trigger_error,
                        "cleanup": self._cleanup_status, "restored": self.session.restored,
                        "error": str(self._failure) if self._failure else None}
        result["receiver"] = receiver
        result["triggers"] = {"nonzero_effects": receiver["trigger_nonzero_effects"]}
        result["output"] = {"peak": receiver["motor_peak"], "values": receiver["motor_output"],
                            "nonzero_writes": receiver["nonzero_packets"],
                            "labels": ("左握把", "右握把", "左扳机", "右扳机")}
        result["native_haptics_translation"] = "stereo 3 kHz PCM -> filtered 1 kHz grip waveforms; DS trigger effects -> position-based trigger vibration"
        return result

    def stop(self):
        self._cancel.set()
        if self._thread:
            self._thread.join(10)
            if self._thread.is_alive():
                self.last_cleanup_error = "接收器停止仍未完成，请保留窗口等待设备恢复"
                raise ReceiverError(self.last_cleanup_error)
            self._thread = None
        with self._lock:
            self._samples.clear()
            self._rumble = (0., 0.)
            self._last_output = (0.,) * 4
