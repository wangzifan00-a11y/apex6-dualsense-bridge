"""Disconnect behavior through fake input/receiver I/O; no physical writes."""
import json
from pathlib import Path
import queue
import threading
import time
import unittest
from unittest.mock import patch

from dsbridge.core.engine import Engine
from dsbridge.core.managed_bridge import ManagedFeedbackBridge
from dsbridge.core.ports import ControllerDisconnected
from dsbridge.controllers.flydigi.apex6.hid import ReceiverDisconnected, ReceiverError, io_error
from dsbridge.controllers.flydigi.apex6.monitor import ReceiverMonitor
from dsbridge.controllers.flydigi.apex6.output import Apex6Output
from dsbridge.controllers.flydigi.apex6.protocol import _ReplyTimeout
from dsbridge.controllers.flydigi.apex6.session import Apex6Session
from test_engine import FakeInput, FakeBackend
from test_receiver_dsp import FakeSession
from test_receiver_protocol import FakeClaim, FakeProtocol, FakeTransport, preserved_test_folder


class DisconnectLifecycleTests(unittest.TestCase):
    def run_engine(self, source, backend):
        events = queue.Queue()
        engine = Engine(source, events, lambda _: backend)
        engine.start(0, 1)
        engine.thread.join(2)
        self.assertFalse(engine.running)
        return list(events.queue)

    def test_disconnect_during_legacy_backend_start_never_reports_started(self):
        source = FakeInput()
        class Backend(FakeBackend):
            stop_calls = 0
            def start(self):
                source.connected = False
            def stop(self):
                self.stop_calls += 1
        backend = Backend()
        events = self.run_engine(source, backend)
        self.assertEqual(backend.stop_calls, 1)
        self.assertNotIn("status", [kind for kind, _ in events])
        self.assertEqual([kind for kind, _ in events], ["disconnected", "session_end", "stopped"])
        self.assertEqual(events[-2][1]["phase"], "disconnected")

    def test_input_close_failure_cannot_skip_final_session_and_stopped(self):
        class Input(FakeInput):
            def close(self):
                raise OSError("COM close failed")
        source = Input()
        class Backend(FakeBackend):
            def start(self):
                source.connected = False
            def stop(self):
                raise OSError("virtual cleanup failed")
        events = self.run_engine(source, Backend())
        self.assertEqual([kind for kind, _ in events][-2:], ["session_end", "stopped"])
        errors = [value for kind, value in events if kind == "error"]
        self.assertTrue(any("virtual cleanup failed" in value for value in errors))
        self.assertTrue(any("COM close failed" in value for value in errors))

    def test_requested_stop_during_initialization_is_not_an_error(self):
        source, events = FakeInput(), queue.Queue()
        class Backend(FakeBackend):
            def start_cancellable(self, check):
                engine.stop()
                check()
        engine = Engine(source, events, lambda _: Backend())
        engine.start(0, 1)
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertEqual([kind for kind, _ in events.queue], ["session_end", "stopped"])
        self.assertEqual(list(events.queue)[0][1]["phase"], "stopped")

    def test_isolated_input_is_released_if_pad_disappears_before_virtual_creation(self):
        calls = []
        online = True
        class Lease:
            error = None
            def start(self):
                calls.append("hidden")
            def stop(self):
                calls.append("input restored")
        class Output:
            error = None
            def start_cancellable(self, check):
                nonlocal online
                calls.append("output start")
                online = False
                check()
            def stop(self):
                calls.append("motors stop")
        class Virtual:
            error = None
            def start(self):
                calls.append("virtual start")
            def stop(self):
                calls.append("virtual stop")
        bridge = ManagedFeedbackBridge(Output(), lambda _: Virtual(), decoder=lambda *_: None, isolation=Lease())
        def check():
            if not online:
                raise ControllerDisconnected("disconnected")
        with self.assertRaises(ControllerDisconnected):
            bridge.start_cancellable(check)
        self.assertEqual(calls, ["hidden", "output start", "motors stop", "virtual stop", "input restored"])

    def test_receiver_monitor_failure_before_arm_ends_worker(self):
        class Monitor:
            error = None
            checks = 0
            def __init__(self, session):
                pass
            def start(self):
                pass
            def stop(self):
                pass
        output = Apex6Output(Path("."), {}, session_factory=FakeSession, monitor_factory=Monitor)
        output.start()
        output.monitor.error = ReceiverDisconnected("link lost")
        output._thread.join(1)
        self.assertFalse(output._thread.is_alive())
        self.assertIsInstance(output.error, ControllerDisconnected)
        self.assertTrue(output.session.restored)
        self.assertEqual(output.session.outputs, [])
        output.stop()

    def test_disconnect_cancels_receiver_setup_before_any_waveform(self):
        class StartingSession(FakeSession):
            def start(self):
                self.cancel.wait(1)
        output = Apex6Output(Path("."), {}, session_factory=StartingSession, monitor_factory=None)
        def disconnected():
            raise ControllerDisconnected("removed during receiver setup")
        with self.assertRaises(ControllerDisconnected):
            output.start_cancellable(disconnected)
        output.stop()
        self.assertTrue(output.session.restored)
        self.assertEqual(output.session.outputs, [])
        self.assertIsNone(output._thread)

    def test_missing_fresh_receiver_response_stops_even_if_receiver_usb_remains(self):
        closed = []
        class Transport:
            def __init__(self, path):
                self.cached_reply = True
            def __enter__(self):
                return self
            def __exit__(self, *_):
                closed.append(True)
            def flush_queue(self):
                self.cached_reply = False
        class Protocol:
            def __init__(self, transport, **_):
                self.transport = transport
            def identity(self):
                if self.transport.cached_reply:
                    return {"uid": "same"}
                raise _ReplyTimeout("no reply after read-only retry")
        session = type("Session", (), {"identity": {"uid": "same"}, "slot": 0,
                                        "transport": type("Handle", (), {"path": "receiver-still-present"})()})()
        monitor = ReceiverMonitor(session, transport_factory=Transport, protocol_factory=Protocol, interval=0.001)
        monitor.start()
        monitor.thread.join(1)
        self.assertIsInstance(monitor.error, ControllerDisconnected)
        self.assertEqual(closed, [True])
        monitor.stop()

    def test_monitor_does_not_mislabel_slot_change_as_disconnect(self):
        class Transport:
            def __init__(self, path): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def flush_queue(self): pass
        class Protocol:
            def __init__(self, *_, **kwargs): pass
            def identity(self): return {"uid": "same"}
            def slot(self): return 1
        session = type("Session", (), {"identity": {"uid": "same"}, "slot": 0,
                                        "transport": type("Handle", (), {"path": "fake"})()})()
        monitor = ReceiverMonitor(session, transport_factory=Transport, protocol_factory=Protocol, interval=0.001)
        monitor.start()
        monitor.thread.join(1)
        self.assertIsInstance(monitor.error, ReceiverError)
        self.assertNotIsInstance(monitor.error, ControllerDisconnected)
        self.assertIn("配置槽", str(monitor.error))
        monitor.stop()

    def test_stationary_pad_with_fresh_identical_identity_stays_connected(self):
        class Transport:
            def __init__(self, path): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def flush_queue(self): pass
        class Protocol:
            def __init__(self, *_, **kwargs): pass
            def identity(self): return {"uid": "same"}
            def slot(self): return 0
        session = type("Session", (), {"identity": {"uid": "same"}, "slot": 0,
                                        "transport": type("Handle", (), {"path": "fake"})()})()
        monitor = ReceiverMonitor(session, transport_factory=Transport, protocol_factory=Protocol, interval=0.001)
        monitor.start()
        deadline = time.monotonic() + 1
        while monitor.checks < 3 and time.monotonic() < deadline:
            time.sleep(0.002)
        monitor.stop()
        self.assertGreaterEqual(monitor.checks, 3)
        self.assertIsNone(monitor.error)

    def test_unplugged_session_keeps_restore_journal_and_releases_handle_and_claim(self):
        with preserved_test_folder() as folder, patch("dsbridge.controllers.flydigi.apex6.session.ReceiverProtocol", FakeProtocol), patch("dsbridge.controllers.flydigi.apex6.session.time.sleep"):
            session = Apex6Session(folder, transport_factory=FakeTransport, claim_factory=FakeClaim)
            session.start("fake")
            handle, claim = session.transport, session.claim
            def disconnected():
                raise ReceiverDisconnected("unplugged")
            session.protocol.identity = disconnected
            session.stop()
            record = json.loads(session.journal_path.read_text(encoding="utf-8"))
            self.assertTrue(record["restore_pending"])
            self.assertIn("unplugged", record["cleanup_errors"])
            self.assertFalse(session.restored)
            self.assertTrue(handle.closed and claim.closed)

    def test_transport_close_failure_does_not_leak_receiver_claim(self):
        session = Apex6Session(Path("."), transport_factory=FakeTransport, claim_factory=FakeClaim)
        class Handle:
            def close(self):
                raise OSError("handle close failed")
        claim = FakeClaim()
        session.claim, session.transport = claim, Handle()
        with self.assertRaisesRegex(OSError, "handle close failed"):
            session.stop()
        self.assertTrue(claim.closed)
        self.assertIsNone(session.transport)

    def test_windows_removal_errors_are_typed_but_other_failures_are_not(self):
        for code in (6, 433, 1167):
            self.assertIsInstance(io_error("read", code), ControllerDisconnected)
        self.assertNotIsInstance(io_error("read", 5), ControllerDisconnected)


if __name__ == "__main__":
    unittest.main()
