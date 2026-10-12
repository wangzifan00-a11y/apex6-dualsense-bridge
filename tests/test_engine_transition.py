"""A planned disconnect must never become an accidental controller reassignment."""
import queue
import threading
import unittest
from dsbridge.core.engine import Engine
from dsbridge.core.ports import BridgeCancelled

STATE = dict(buttons=0, left_trigger=0, right_trigger=0, lx=0, ly=0, rx=0, ry=0)


class EngineTransitionTests(unittest.TestCase):
    def scenario(self, *, fail=None, managed=False):
        actions, ready = [], threading.Event()

        class Input:
            selected = 0
            def get_state(self, slot):
                return STATE.copy() if slot == self.selected else None
            def prepare_input(self, slot):
                actions.append(("map", slot))
                return lambda state: state
            def validate_input(self, slot):
                return slot == self.selected
            def set_rumble(self, slot, left, right):
                actions.append(("rumble", slot, left, right))
            def close(self):
                actions.append("close")

        class Backend:
            error = None
            def start_cancellable(self, check):
                actions.append("backend_start")
                if managed:
                    self_test.assertEqual(self.gain, 0)
                check()
                if fail == "backend":
                    raise RuntimeError("backend failure")
            def update(self, state):
                ready.set()
            def poll_feedback(self):
                return (0, 0)
            def set_gain(self, gain):
                self.gain = gain
            def stop(self):
                actions.append("backend_stop")
                if fail == "reported_cleanup":
                    self.last_cleanup_error = "motor restore pending"

        self_test = self
        source, backend = Input(), Backend()

        class Transition:
            error = None
            slot = 2
            def start_cancellable(self, check):
                actions.append("transition_start")
                source.selected = None
                check()  # planned absence must not abort startup
                if fail == "transition":
                    raise RuntimeError("transition failure")
                if fail == "cancel":
                    raise BridgeCancelled()
                source.selected = 2
            def create_backend(self):
                actions.append("backend_create")
                return backend
            def stop(self):
                actions.append("transition_stop")
                if fail == "restore":
                    raise RuntimeError("restore failure")
            def status(self):
                return {"phase": "test"}

        transition = Transition()
        engine = Engine(source, queue.Queue(), lambda mode: self.fail("legacy backend used"),
                        transition_factory=lambda *args: transition)
        engine.gain = 0
        engine.start(0, 5 if managed else 1)
        if fail not in ("backend", "transition", "cancel"):
            self.assertTrue(ready.wait(2))
            engine.stop()
        engine.thread.join(3)
        self.assertFalse(engine.running)
        return actions, list(engine.events.queue), transition

    def test_start_rebinds_before_mapping_and_stops_backend_before_restoring(self):
        actions, events, _ = self.scenario()
        self.assertEqual(actions[:4], ["transition_start", ("map", 2), "backend_create", "backend_start"])
        self.assertLess(actions.index("backend_stop"), actions.index("transition_stop"))
        writes = [a for a in actions if isinstance(a, tuple) and a[0] == "rumble"]
        self.assertTrue(writes)
        self.assertTrue(all(a[1] == 2 for a in writes))
        self.assertEqual(next(data for kind, data in events if kind == "session_end")["slot"], 2)

    def test_failed_or_cancelled_rebind_never_writes_old_or_reused_slot(self):
        for failure in ("transition", "cancel"):
            with self.subTest(failure=failure):
                actions, _, _ = self.scenario(fail=failure)
                self.assertEqual(actions, ["transition_start", "transition_stop", "close"])

    def test_failed_backend_still_restores_transition(self):
        actions, events, _ = self.scenario(fail="backend")
        self.assertLess(actions.index("backend_stop"), actions.index("transition_stop"))
        self.assertTrue(any(kind == "error" for kind, _ in events))

    def test_restore_failure_is_reported_and_not_a_successful_stop(self):
        _, events, _ = self.scenario(fail="restore")
        final = next(data for kind, data in events if kind == "session_end")
        self.assertEqual(final["phase"], "failed")
        self.assertIn("restore failure", final["error"])

    def test_managed_receiver_never_sends_xinput_rumble_during_transition(self):
        actions, _, _ = self.scenario(managed=True)
        self.assertFalse(any(isinstance(a, tuple) and a[0] == "rumble" for a in actions))

    def test_reported_cleanup_failure_keeps_restore_order_and_fails_session(self):
        actions, events, _ = self.scenario(fail="reported_cleanup", managed=True)
        self.assertLess(actions.index("backend_stop"), actions.index("transition_stop"))
        self.assertLess(actions.index("transition_stop"), actions.index("close"))
        final = next(data for kind, data in events if kind == "session_end")
        self.assertEqual(final["phase"], "failed")
        self.assertIn("motor restore pending", final["error"])
        self.assertTrue(any(kind == "error" and "motor restore pending" in data for kind, data in events))
        self.assertEqual(events[-1], ("stopped", None))


if __name__ == "__main__":
    unittest.main()
