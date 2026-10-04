import queue
import json
from pathlib import Path
import time
import unittest
import uuid
from app import Engine, FeedbackSessionLog, scale_feedback

STATE = dict(buttons=0, left_trigger=0, right_trigger=0, lx=0, ly=0, rx=0, ry=0)


class FakeInput:
    def __init__(self):
        self.writes = []
        self.connected = True
    def get_state(self, index):
        return STATE.copy() if self.connected else None
    def set_rumble(self, index, left, right):
        self.writes.append((index, left, right))


class FakeBackend:
    error = None
    feedback_polled = False
    def start(self):
        pass
    def update(self, state):
        pass
    def poll_feedback(self):
        if self.feedback_polled:
            return None
        self.feedback_polled = True
        return (0.4, 0.8)
    def stop(self):
        pass


class EngineTests(unittest.TestCase):
    def test_receiver_mode_never_overwrites_waveforms_with_xinput_even_at_shutdown(self):
        class ReceiverInput(FakeInput):
            def get_state(self, index):
                return super().get_state(index) if index == 0 else None
        class ReceiverBackend(FakeBackend):
            def set_gain(self, gain):
                self.gain = gain
        xi, backend = ReceiverInput(), ReceiverBackend()
        engine = Engine(xi, queue.Queue(), lambda *_: backend)
        engine.start(0, 5)
        time.sleep(0.03)
        self.finish(engine)
        self.assertEqual(xi.writes, [])
        self.assertEqual(backend.gain, engine.gain)

    def make(self, mode):
        xi = FakeInput()
        backend = FakeBackend()
        engine = Engine(xi, queue.Queue(), lambda *_: backend)
        engine.start(0, mode)
        time.sleep(0.035)
        return xi, engine, backend
    def finish(self, engine):
        engine.stop()
        engine.thread.join(2)
        self.assertFalse(engine.running)
    def test_removed_modes_rejected_before_any_controller_operation(self):
        class UntouchedInput:
            def get_state(self, index):
                raise AssertionError("removed mode touched the physical controller")
        for mode in (0, 2, 3, 4):
            engine = Engine(UntouchedInput(), queue.Queue(), lambda *_: self.fail("removed mode created a backend"))
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "仅支持"):
                engine.start(0, mode)
            self.assertFalse(engine.running)

    def test_ps4_cannot_be_created_through_backend_factory(self):
        engine = Engine(FakeInput(), queue.Queue(), lambda *_: self.fail("PS4 backend created"))
        with self.assertRaisesRegex(ValueError, "不支持"):
            engine._make_backend(2)
    def test_feedback_gain_and_shutdown_stop(self):
        xi, engine, _ = self.make(1)
        self.finish(engine)
        self.assertIn((0, 0.4 * 0.7, 0.8 * 0.7), xi.writes)
        self.assertNotIn((0, 0, 0), xi.writes[:-1])
        self.assertEqual(xi.writes[-1], (0, 0, 0))

    def test_session_records_confirm_actual_motor_writes_without_logging_input(self):
        xi, engine, _ = self.make(1)
        self.finish(engine)
        records = []
        while not engine.events.empty():
            kind, value = engine.events.get_nowait()
            if kind == "session_end":
                records.append(value)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["phase"], "stopped")
        self.assertEqual(record["output_nonzero_writes"], len(xi.writes) - 1)
        self.assertEqual(record["output_peak"], (0.4 * 0.7, 0.8 * 0.7))
        self.assertNotIn("state", record)
        self.assertIsNone(record["error"])
    def test_backend_failure_stops_motors(self):
        xi, engine, backend = self.make(1)
        backend.error = RuntimeError("connection lost")
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertEqual(xi.writes[-1], (0, 0, 0))
    def test_disconnect_stops_and_does_not_select_another_slot(self):
        xi, engine, _ = self.make(1)
        xi.connected = False
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertEqual(xi.writes[-1], (0, 0, 0))
        self.assertTrue(all(w[0] == 0 for w in xi.writes))
    def test_scale_clamps_nonfinite_and_extreme_values(self):
        self.assertEqual(scale_feedback((float("nan"), float("inf")), 1), (0, 0))
        self.assertEqual(scale_feedback((-1, 2), 2), (0, 1))

    def test_final_session_record_is_saved_even_inside_throttled_interval(self):
        path = Path(__file__).resolve().parents[1] / "测试记录" / ("反馈统计-" + uuid.uuid4().hex + ".json")
        path.parent.mkdir(exist_ok=True)
        journal = FeedbackSessionLog(path)
        journal.write({"session_id": "first", "phase": "running"})
        journal.write({"session_id": "first", "phase": "stopped"}, final=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["phase"], "stopped")
        # A new start must replace stale statistics immediately, even within 1s.
        journal.write({"session_id": "second", "phase": "running"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["session_id"], "second")


if __name__ == "__main__":
    unittest.main()
