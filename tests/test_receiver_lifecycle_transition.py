"""Packaged lifecycle diagnostics must exercise the production transition path."""
import json
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch
import uuid

from dsbridge.core.modes import PROFILES
from dsbridge.diagnostics import receiver_lifecycle as diagnostic
from dsbridge.runtime.paths import RuntimePaths


class Input:
    slots = range(4)

    def __init__(self):
        self.closed = 0

    def get_state(self, index):
        return {"left_trigger": 0, "right_trigger": 0} if index == 0 else None

    def close(self):
        self.closed += 1


class Backend:
    isolation = None
    last_cleanup_error = None

    def __init__(self, order):
        self.order = order
        self.viiper = types.SimpleNamespace(process=types.SimpleNamespace(pid=901),
                                            start=lambda: None, start_cancellable=lambda check: check())
        self._failure = None
        self.gain = None
        self.stopped = False
        self.gain_at_start = None

    @property
    def error(self):
        return self._failure

    def set_gain(self, value):
        self.gain = value

    def start_cancellable(self, check):
        self.order.append("backend.start")
        self.gain_at_start = self.gain
        check()
        self.viiper.start_cancellable(check)

    def update(self, state):
        pass

    def poll_feedback(self):
        return (0, 0)

    def status(self):
        return {"receiver": {"motor_nonzero_writes": [0, 0, 0, 0]}}

    def stop(self):
        self.order.append("backend.stop")
        self.stopped = True
        self.viiper.process = None


class Transition:
    slot = 0
    error = None

    def __init__(self, backend, order):
        self.backend, self.order = backend, order
        self.phase = "new"

    def start_cancellable(self, check):
        check()
        self.order.append("transition.start")
        self.phase = "ready"

    def stop(self):
        if not self.backend.stopped:
            raise RuntimeError("input restored before output stopped")
        self.order.append("transition.stop")
        self.phase = "stopped"

    def status(self):
        finished = self.phase == "stopped"
        return {"phase": self.phase, "slot": 0, "refresh_requested": True,
                "lease": {"phase": "finished" if finished else "ready", "guard_pid": 902,
                          "restored": finished, "input_visibility_restored": finished,
                          "refresh_pending": not finished}}


def visible(count):
    return {"libraries": {"xinput1_4": [{"connected": bool(count)}],
                          "xinput9_1_0": [{"connected": bool(count)}]}}


class ReceiverLifecycleTransitionTests(unittest.TestCase):
    def run_case(self, case):
        # Keep test evidence; never recursively delete workspace files.
        base = Path(__file__).resolve().parents[1] / "测试记录" / ("生命周期路径-" + uuid.uuid4().hex)
        paths = RuntimePaths(base, base).prepare()
        order = []
        input_port = Input()
        backend = Backend(order)
        transition = Transition(backend, order)
        context = MagicMock()
        context.registry.profiles = PROFILES
        context.create_input.return_value = input_port
        context.create_backend.return_value = backend
        context.create_transition.return_value = transition
        audio = MagicMock()
        audio.__enter__.return_value.snapshot.return_value = {"defaults": {"render": "speakers", "capture": "mic"}}
        reports = [visible(1), visible(1)] if case == "start-failure" else [visible(1), visible(0), visible(1)]
        with patch.object(diagnostic.sys, "frozen", True, create=True), \
             patch("dsbridge.application.context.ApplicationContext", return_value=context), \
             patch("dsbridge.platform.windows.audio.WindowsAudio", return_value=audio), \
             patch("dsbridge.diagnostics.hidhide.read_config", return_value={"hidden_devices": []}), \
             patch.object(diagnostic, "visibility", side_effect=reports), \
             patch.object(diagnostic.time, "sleep"):
            diagnostic.main(paths, case)
        report = json.loads((base / ("input-lifecycle-" + case + ".json")).read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertEqual(report["guard_pid"], 902)
        self.assertTrue(report["input_transition"]["lease"]["restored"])
        self.assertFalse(report["input_transition"]["lease"]["refresh_pending"])
        self.assertEqual(backend.gain_at_start, 0)
        context.create_input.assert_called_once_with(5)
        context.create_backend.assert_called_once_with(5, input_isolated=True)
        self.assertIs(context.create_transition.call_args.args[0], input_port)
        self.assertEqual(order, ["transition.start", "backend.start", "backend.stop", "transition.stop"])
        self.assertGreater(input_port.closed, 0)
        return report

    def test_normal_uses_production_transition_and_restores_after_backend(self):
        self.run_case("normal")

    def test_start_failure_keeps_fault_injection_and_real_transition_cleanup(self):
        report = self.run_case("start-failure")
        self.assertTrue(any("显式启动失败自检" in value for value in report["startup_errors"]))

    def test_runtime_failure_uses_same_restore_path(self):
        self.run_case("runtime-failure")

    def test_native_diagnostic_cannot_run_from_source(self):
        with patch.object(diagnostic.sys, "frozen", False, create=True):
            with self.assertRaisesRegex(RuntimeError, "打包程序"):
                diagnostic.main(Path("."), "normal")


if __name__ == "__main__":
    unittest.main()
