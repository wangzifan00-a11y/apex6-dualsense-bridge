import math
from pathlib import Path
import struct
import threading
import time
import unittest

from receiver_backend import ReceiverFeedback
from receiver_dsp import (StereoDecimator, TriggerEffect, TriggerRenderer,
                          TriggerRouter, decode_trigger)
from test_viiper_backend import carrier_feedback, rumble_feedback, atomic
from viiper_backend import ViiperBackend


class FakeSession:
    def __init__(self, base):
        self.identity = {"uid": "test"}
        self.transport = None
        self.calls, self.outputs = [], []
        self.restored = False
        self.cleanup_errors = []
        self.journal = None
    def start(self):
        self.calls.append(("start", threading.get_ident()))
    def send(self, left, right, trigger, **kwargs):
        self.calls.append(("write", threading.get_ident()))
        self.outputs.append((left, right, trigger, kwargs))
    def neutral(self):
        pass
    def check_identity(self):
        self.calls.append(("check", threading.get_ident()))
    def stop(self):
        self.calls.append(("stop", threading.get_ident()))
        self.restored = True


class FakeViiper:
    error = None
    def __init__(self, base, config, **kwargs):
        self.sink = kwargs["feedback_sink"]
        self.stopped = False
    def start(self):
        pass
    def update(self, state):
        pass
    def status(self):
        return {}
    def stop(self):
        self.stopped = True


def backend():
    return ReceiverFeedback(Path("."), {}, session_factory=FakeSession, viiper_factory=FakeViiper,
                            monitor_factory=None, isolation_factory=None)


class DSPTests(unittest.TestCase):
    def test_stereo_decimation_preserves_chunk_order_and_lane_isolation(self):
        signal = [70 * math.sin(2 * math.pi * 100 * n / 3000) for n in range(192)]
        whole = StereoDecimator().feed(signal, [0] * len(signal))
        stream = StereoDecimator()
        pieces = []
        for i in range(0, len(signal), 32):
            pieces.extend(stream.feed(signal[i:i + 32], [0] * 32))
        self.assertEqual(whole, pieces)
        self.assertEqual(len(whole), 64)
        self.assertTrue(all(pair[1] == 0 for pair in whole))
    def test_filter_suppresses_aliasing_before_downsampling(self):
        def power(frequency):
            signal = [math.sin(2 * math.pi * frequency * n / 3000) for n in range(3000)]
            values = StereoDecimator().feed(signal, signal)[40:]
            return math.sqrt(sum(pair[0] ** 2 for pair in values) / len(values))
        self.assertGreater(power(100), 0.65)
        self.assertLess(power(1100), 0.01)
    def test_zero_frequency_and_empty_zone_masks_are_real_stop_commands(self):
        for data in (bytes.fromhex("2601000100000000000000"), bytes.fromhex("2100000100000000000000")):
            self.assertEqual(decode_trigger(data).kind, "off")
        with self.assertRaises(ValueError):
            decode_trigger(bytes.fromhex("2100040100000000000000"))
    def test_weapon_break_is_one_pulse_until_released(self):
        renderer = TriggerRenderer()
        renderer.set_effect(TriggerEffect(kind="weapon", start=50, end=150, strength=8))
        renderer.render(0)
        renderer.render(100)
        self.assertTrue(any(renderer.render(180)))
        self.assertTrue(any(renderer.render(180)))
        self.assertFalse(any(renderer.render(180)))
        self.assertFalse(any(renderer.render(180)))
        renderer.render(0)
        self.assertTrue(any(renderer.render(180)))
    def test_distinct_triggers_alternate_and_identical_triggers_use_both(self):
        route = TriggerRouter()
        left, right = (10,) * 8, (-20,) * 8
        self.assertEqual(route.choose(left, right)[:2], (0, left))
        self.assertEqual(route.choose(left, right)[:2], (1, right))
        self.assertEqual(route.choose(left, left)[:2], (2, left))
        self.assertEqual(route.choose((0,) * 8, (0,) * 8), (2, (0,) * 8, False))

    def test_waveform_path_drops_stale_backlog_and_has_a_bounded_queue(self):
        instance = backend()
        frame = carrier_feedback(60, 0)
        for n in range(30):
            instance.on_feedback(0x84, frame, 10 + n / 100)
        self.assertLessEqual(len(instance._samples), 40)
        self.assertGreater(instance._discarded, 0)
        left, right, *_ = instance._render(11)
        self.assertFalse(any(left) or any(right))
    def test_led_reports_do_not_cancel_existing_rumble_or_trigger_effects(self):
        instance = backend()
        effect = bytearray(rumble_feedback(0, 0, 8))
        effect[28 + 22:28 + 33] = bytes.fromhex("2101000100000000000000")
        instance.on_feedback(0x81, bytes(effect), time.monotonic())
        instance.on_feedback(0x81, rumble_feedback(150, 0), time.monotonic())
        led = bytearray(474)
        led[28] = 2
        led[30] = 4
        instance.on_feedback(0x81, bytes(led), time.monotonic())
        self.assertEqual(instance._rumble, (150 / 255, 0))
        self.assertEqual(instance._triggers[0].effect.kind, "texture")
    def test_pcm_callback_does_not_replay_hid_snapshot_or_speaker_audio(self):
        seen = []
        instance = ViiperBackend(feedback_sink=lambda *args: seen.append(args))
        frame = carrier_feedback(30, -30)
        instance._apply_feedback(0x81, frame)
        instance._apply_feedback(0x83, atomic(frame, b"\xff" * 1920))
        instance._apply_feedback(0x84, frame)
        instance._apply_feedback(0x83, atomic(frame, b"\xff" * 1920))
        self.assertEqual([row[0] for row in seen], [0x81, 0x83, 0x84])
        self.assertTrue(all(len(row[1]) == 474 for row in seen))
    def test_receiver_io_and_restore_share_one_worker_owner(self):
        instance = backend()
        instance.start()
        instance.on_feedback(0x81, rumble_feedback(150, 70), time.monotonic())
        time.sleep(0.04)
        instance.stop()
        owners = {owner for _, owner in instance.session.calls}
        self.assertEqual(len(owners), 1)
        self.assertNotIn(threading.get_ident(), owners)
        self.assertTrue(instance.session.restored and instance.viiper.stopped)
        self.assertTrue(any(any(lane) for packet in instance.session.outputs for lane in packet[:2]))

    def test_failed_neutral_still_joins_monitor_then_restores_session(self):
        class FailedNeutralSession(FakeSession):
            def start(self):
                super().start()
                self.transport = object()
            def neutral(self):
                self.calls.append(("neutral", threading.get_ident()))
                raise RuntimeError("device write failed")
        class Monitor:
            error = None
            def __init__(self, session):
                self.session = session
            def start(self):
                pass
            def stop(self):
                self.session.calls.append(("monitor stopped", threading.get_ident()))
        instance = ReceiverFeedback(Path("."), {}, session_factory=FailedNeutralSession,
                                    viiper_factory=FakeViiper, monitor_factory=Monitor, isolation_factory=None)
        instance.start()
        instance.stop()
        self.assertEqual([name for name, _ in instance.session.calls][-3:],
                         ["neutral", "monitor stopped", "stop"])
        self.assertTrue(instance.session.restored and instance.viiper.stopped)
        self.assertIn("device write failed", instance.last_cleanup_error)

    def test_monitor_failure_stops_waveforms_and_restores_session(self):
        class Monitor:
            error = RuntimeError("pad identity changed")
            def __init__(self, session):
                self.session = session
            def start(self):
                pass
            def stop(self):
                self.session.calls.append(("monitor stopped", threading.get_ident()))
        instance = ReceiverFeedback(Path("."), {}, session_factory=FakeSession,
                                    viiper_factory=FakeViiper, monitor_factory=Monitor, isolation_factory=None)
        instance.start()
        instance._thread.join(1)
        self.assertFalse(instance._thread.is_alive())
        instance.stop()
        self.assertEqual([name for name, _ in instance.session.calls][-2:],
                         ["monitor stopped", "stop"])
        self.assertEqual(instance.session.outputs, [])
        self.assertTrue(instance.session.restored and instance.viiper.stopped)
        self.assertIn("pad identity changed", str(instance.error))


if __name__ == "__main__":
    unittest.main()
