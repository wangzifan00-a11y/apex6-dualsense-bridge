"""Startup ordering and recovery during planned controller disconnection."""
import queue
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dsbridge.core.connection import ControllerConnection
from dsbridge.core.ports import BridgeCancelled
from dsbridge.platform.windows.input_transition import InputTransition
from dsbridge.platform.windows.input_transition_targets import UnsupportedInputTargets
from dsbridge.runtime.paths import RuntimePaths


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Port:
    slots = range(4)

    def __init__(self, history, values):
        self.history = history
        self.selected = ControllerConnection("receiver", device_id=r"HID\VID_37D7&PID_2502&IG_00\selected")
        self.values = iter(values)
        self.rebind_calls = 0
        self.expected = self.selected
        self.rebound_connections = []

    def connection(self, index):
        self.history.append("connection")
        return self.selected

    def rebind(self, selected):
        if selected is not self.expected:
            raise AssertionError("A different physical controller was requested")
        self.rebound_connections.append(selected)
        self.history.append("rebind")
        self.rebind_calls += 1
        return next(self.values, None)

    def get_state(self, _index):
        raise AssertionError("Planned PnP disconnection must not query the old slot")

    def set_rumble(self, *_args):
        raise AssertionError("Input transition must not send motor commands")


class Lease:
    def __init__(self, history):
        self.history = history
        self.error = None
        self.start_error = self.stop_error = None
        self.stops = 0

    def start_cancellable(self, check):
        self.history.append("lease_start")
        check()
        if self.start_error:
            raise self.start_error

    def stop(self):
        self.history.append("lease_stop")
        self.stops += 1
        if self.stop_error:
            raise self.stop_error

    def status(self):
        return {"phase": "ready" if not self.stops else "finished", "restored": bool(self.stops),
                "guard_pid": 111, "targets": {"device_id": "private id"},
                "connection": {"device_id": "private id"}, "state": {"buttons": 4096}}


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.history = []
        self.events = queue.Queue()
        self.clock = Clock()
        self.lease = Lease(self.history)
        self.targets = object()
        self.port = Port(self.history, [None, None, 2])
        self.lease.current_connection = self.port.selected
        self.paths = RuntimePaths(Path.cwd(), Path.cwd())
        self.probe_reply = {"state": "held", "complete": False,
                            "clients": [{"pid": 123, "name": "consumer.exe", "source": "selected_hid"}]}
        def resolve(connection):
            self.assertIs(connection, self.port.selected)
            self.history.append("resolve")
            return self.targets
        def probe(connection, **_kwargs):
            self.assertIs(connection, self.port.selected)
            self.history.append("probe")
            return self.probe_reply
        def factory(paths, connection, targets):
            self.assertIs(connection, self.port.selected)
            self.assertIs(targets, self.targets)
            self.history.append("lease_create")
            return self.lease
        def ensure(paths, notify, check):
            self.history.append("ensure_service")
            notify("依赖已就绪")
            check()
        self.transition = InputTransition(self.paths, self.port, 0, 5, self.events, ensure_service=ensure)
        self.patches = [patch("dsbridge.platform.windows.input_transition.resolve_input_targets", side_effect=resolve),
                        patch("dsbridge.platform.windows.input_transition.probe_controller_usage", side_effect=probe),
                        patch("dsbridge.platform.windows.input_transition.HotSwitchLease", side_effect=factory),
                        patch("dsbridge.platform.windows.input_transition.time.monotonic", self.clock.monotonic),
                        patch("dsbridge.platform.windows.input_transition.time.sleep", self.clock.sleep)]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_live_old_handle_orders_safe_switch_before_unique_rebind(self):
        self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.history[:6], ["connection", "resolve", "probe", "ensure_service", "lease_create", "lease_start"])
        self.assertEqual(self.transition.slot, 2)
        self.assertEqual(self.transition.status()["phase"], "ready")
        self.assertEqual(self.port.rebind_calls, 3)
        self.assertIsNone(self.transition.error)
        self.assertEqual(self.lease.stops, 0)
        self.transition.stop()
        self.transition.stop()
        self.assertEqual(self.lease.stops, 1)
        self.assertEqual(self.transition.status()["phase"], "stopped")

    def test_unknown_or_empty_usage_still_refreshes_automatically(self):
        for reported_state in ("unknown", "none"):
            with self.subTest(state=reported_state):
                self.probe_reply = {"state": reported_state, "clients": [], "complete": True}
                self.port.values = iter([0])
                transition = InputTransition(self.paths, self.port, 0, 1, self.events,
                                             ensure_service=self.transition.ensure_service)
                transition.start_cancellable(lambda: None)
                self.assertTrue(transition.status()["refresh_requested"])
                self.assertEqual(transition.status()["usage"]["state"], "unknown")
                transition.stop()

    def test_lease_validated_current_leaf_is_used_for_every_rebind_attempt(self):
        current = replace(self.port.selected, device_id=r"HID\VID_37D7&PID_2502&IG_01\returned")
        self.lease.current_connection = self.port.expected = current
        self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.transition.slot, 2)
        self.assertIs(self.transition.current_connection, current)
        self.assertEqual(self.port.rebound_connections, [current, current, current])
        self.assertEqual(self.port.selected.device_id, r"HID\VID_37D7&PID_2502&IG_00\selected")

    def test_missing_or_different_device_family_from_guard_never_rebinds(self):
        for current in (None, replace(self.port.selected, kind="bluetooth"),
                        replace(self.port.selected, vendor_id=0x1234)):
            with self.subTest(current=current):
                self.lease.current_connection = current
                transition = InputTransition(self.paths, self.port, 0, 5, self.events,
                                             ensure_service=self.transition.ensure_service)
                with self.assertRaisesRegex(RuntimeError, "已验证的当前手柄连接"):
                    transition.start_cancellable(lambda: None)
        self.assertEqual(self.port.rebind_calls, 0)
        self.assertEqual(self.lease.stops, 3)

    def test_unsupported_tree_fails_before_probe_installer_guard_or_mutation(self):
        self.mocks[0].side_effect = UnsupportedInputTargets("unsupported branch")
        with self.assertRaises(UnsupportedInputTargets):
            self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.history, ["connection"])
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()
        self.assertEqual(self.lease.stops, 0)

    def test_first_cancel_never_touches_hardware_or_installer(self):
        def cancelled():
            raise BridgeCancelled()
        with self.assertRaises(BridgeCancelled):
            self.transition.start_cancellable(cancelled)
        self.assertEqual(self.history, [])
        self.assertEqual(self.transition.status()["phase"], "cancelled")

    def test_unavailable_usage_probe_is_unknown_and_still_refreshes(self):
        self.mocks[1].side_effect = OSError("probe unavailable")
        self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.transition.status()["usage"]["state"], "unknown")
        self.assertEqual(self.history.count("lease_start"), 1)

    def test_service_failure_never_creates_guard_or_restarts_device(self):
        def unavailable(*_args):
            raise RuntimeError("service unavailable")
        self.transition.ensure_service = unavailable
        with self.assertRaisesRegex(RuntimeError, "service unavailable"):
            self.transition.start_cancellable(lambda: None)
        self.mocks[2].assert_not_called()
        self.assertEqual(self.lease.stops, 0)

    def test_cancel_during_rebind_restores_lease(self):
        def cancelled():
            if self.port.rebind_calls:
                raise BridgeCancelled()
        with self.assertRaises(BridgeCancelled):
            self.transition.start_cancellable(cancelled)
        self.assertEqual(self.lease.stops, 1)
        self.assertEqual(self.transition.slot, 0)
        self.assertEqual(self.transition.status()["phase"], "cancelled")

    def test_rebind_timeout_is_bounded_and_restores_input(self):
        self.port.values = iter([])
        with self.assertRaisesRegex(RuntimeError, "未能重新定位同一只手柄"):
            self.transition.start_cancellable(lambda: None)
        self.assertAlmostEqual(self.clock.now, 12.0)
        self.assertEqual(self.lease.stops, 1)
        self.assertEqual(self.transition.status()["phase"], "failed")

    def test_invalid_rebound_slot_never_selects_another_controller(self):
        for value in (True, "1", 4, -1):
            with self.subTest(value=value):
                self.port.values = iter([value])
                transition = InputTransition(self.paths, self.port, 0, 1, self.events,
                                             ensure_service=self.transition.ensure_service)
                with self.assertRaisesRegex(RuntimeError, "无效的手柄槽位"):
                    transition.start_cancellable(lambda: None)
                self.assertEqual(transition.slot, 0)

    def test_late_rebind_cannot_extend_the_twelve_second_window(self):
        def late(_connection):
            self.clock.now += 13
            return 2
        self.port.rebind = late
        with self.assertRaisesRegex(RuntimeError, "未能重新定位同一只手柄"):
            self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.lease.stops, 1)
        self.assertEqual(self.transition.slot, 0)

    def test_guard_failure_during_rebind_stops_session(self):
        original = self.port.rebind
        failure = RuntimeError("guard failed")
        def fail_after_first(selected):
            result = original(selected)
            self.lease.error = failure
            return result
        self.port.rebind = fail_after_first
        with self.assertRaisesRegex(RuntimeError, "guard failed"):
            self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.lease.stops, 1)
        self.assertIs(self.transition.error, failure)

    def test_partially_started_guard_is_stopped_and_failed_cleanup_can_retry(self):
        self.lease.start_error = RuntimeError("start failed")
        self.lease.stop_error = RuntimeError("restore failed")
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.lease.stops, 1)
        self.assertEqual(str(self.transition.error), "restore failed")
        self.lease.stop_error = None
        self.transition.stop()
        self.assertEqual(self.lease.stops, 2)
        self.assertEqual(self.transition.status()["phase"], "stopped")

    def test_status_contains_no_controller_paths_raw_states_or_guard_tokens(self):
        self.probe_reply["clients"][0]["device_id"] = "private id"
        self.probe_reply["state_snapshot"] = {"buttons": 42}
        self.transition.start_cancellable(lambda: None)
        report = self.transition.status()
        text = repr(report)
        for secret in ("private id", "buttons", "targets", "connection", "selected"):
            self.assertNotIn(secret, text.replace("selected_hid", "input"))
        report["usage"]["clients"][0]["name"] = "changed externally"
        self.assertEqual(self.transition.status()["usage"]["clients"][0]["name"], "consumer.exe")

    def test_extended_profile_is_not_silently_intercepted(self):
        transition = InputTransition(self.paths, self.port, 0, "plugin", self.events,
                                     ensure_service=self.transition.ensure_service)
        with self.assertRaises(ValueError):
            transition.start_cancellable(lambda: None)
        self.assertEqual(self.history, [])

    def test_used_transition_cannot_double_restart(self):
        self.transition.start_cancellable(lambda: None)
        with self.assertRaisesRegex(RuntimeError, "已经使用"):
            self.transition.start_cancellable(lambda: None)
        self.assertEqual(self.history.count("lease_start"), 1)


if __name__ == "__main__":
    unittest.main()
