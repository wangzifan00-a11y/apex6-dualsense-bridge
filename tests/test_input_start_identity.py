"""No mapped frame may reach a replacement pad's reused slot at startup."""
from dataclasses import replace
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dsbridge.controllers.registry import InputRouter
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.engine import Engine
from dsbridge.platform.windows.connection import matches_selected_input
from dsbridge.platform.windows.xinput import XInput


IDENTITY = {"vendor_id": 0x37D7, "product_id": 0x2502, "flags": 0}
DEVICE = ControllerConnection("receiver", device_id=r"HID\VID_37D7&PID_2502&IG_01\ORIGINAL",
                              vendor_id=0x37D7, product_id=0x2502, model="flydigi.apex6pro")


def device_row(identifier=DEVICE.device_id, *, service=""):
    return {"instance_id": identifier, "name": "HID-compliant game controller",
            "ancestors": [{"instance_id": identifier, "name": "HID-compliant game controller", "service": service},
                          {"instance_id": r"USB\VID_37D7&PID_2502", "name": "", "service": "usbccgp"}]}


class PhysicalIdentityTests(unittest.TestCase):
    def test_same_unique_input_is_recognized_without_vendor_query(self):
        with patch("dsbridge.controllers.flydigi.apex6.connection.query_connection") as query:
            self.assertTrue(matches_selected_input(IDENTITY, DEVICE, [device_row()]))
            self.assertTrue(matches_selected_input(IDENTITY, DEVICE, [device_row(DEVICE.device_id.lower())]))
            query.assert_not_called()

    def test_missing_ambiguous_virtual_or_replaced_input_is_rejected(self):
        for rows in ([], [device_row("HID\\OTHER")], [device_row(), device_row("HID\\OTHER")],
                     [device_row(service="usbip2_ude")]):
            self.assertFalse(matches_selected_input(IDENTITY, DEVICE, rows))
        self.assertFalse(matches_selected_input(None, DEVICE, [device_row()]))
        self.assertFalse(matches_selected_input(dict(IDENTITY, vendor_id=0x045E), DEVICE, [device_row()]))
        self.assertFalse(matches_selected_input(IDENTITY, replace(DEVICE, device_id=""), [device_row()]))

    def test_xinput_recheck_only_reads_identity_and_pnp(self):
        source = XInput.__new__(XInput)
        source.identity = Mock(return_value=IDENTITY)
        with patch("dsbridge.platform.windows.connection.present_gamepads", return_value=[device_row()]) as nodes, \
                patch("dsbridge.controllers.flydigi.apex6.connection.query_connection") as vendor:
            self.assertTrue(source.validate_connection(0, DEVICE))
            source.identity.assert_called_once_with(0)
            nodes.assert_called_once_with(0x37D7, 0x2502)
            vendor.assert_not_called()


class RouterIdentityTests(unittest.TestCase):
    def test_captures_selected_slot_and_resets_on_each_preparation(self):
        source = SimpleNamespace(connection=Mock(return_value=DEVICE),
                                 validate_connection=Mock(return_value=True), close=Mock())
        router = InputRouter(None)
        router._key, router._input = "xinput", source
        self.assertTrue(router.prepare_input(2)({"buttons": 0x20})["touchpad_click"])
        self.assertTrue(router.validate_input(2))
        source.validate_connection.assert_called_once_with(2, DEVICE)
        self.assertFalse(router.validate_input(1))
        source.connection.return_value = replace(DEVICE, model="")
        router.prepare_input(2)
        self.assertTrue(router.validate_input(2))
        # Hot switching must retain physical identity even without a model-specific mapping.
        self.assertEqual(source.validate_connection.call_count, 2)
        source.validate_connection.assert_called_with(2, replace(DEVICE, model=""))

    def test_extensions_and_legacy_providers_remain_compatible(self):
        router = InputRouter(None)
        router._key, router._input = "extension.pad", SimpleNamespace(connection=Mock())
        router.prepare_input("custom")
        self.assertTrue(router.validate_input("custom"))
        router._input.connection.assert_not_called()
        router._key, router._input = "xinput", SimpleNamespace(connection=lambda _: DEVICE)
        router.prepare_input(0)
        self.assertTrue(router.validate_input(0))


class InitializationIdentityTests(unittest.TestCase):
    def run_startup(self, changed):
        checked, reached = [], threading.Event()
        physical = SimpleNamespace(identity=DEVICE)
        frames, writes = [], []

        class Source:
            def get_state(self, slot):
                # Both physical devices are continuously readable in slot 0:
                # this specifically defeats a simple disconnected-state check.
                return {"buttons": 0x20} if slot == 0 else None
            def connection(self, slot):
                return physical.identity
            def validate_connection(self, slot, expected):
                checked.append((slot, expected))
                return physical.identity == expected
            def set_rumble(self, slot, left, right):
                writes.append((slot, left, right))
                return True
            def close(self):
                pass

        class Backend:
            error = None
            stopped = False
            def start(self):
                if changed:
                    physical.identity = replace(DEVICE, device_id="HID\\REPLACEMENT", model="")
            def update(self, state):
                frames.append(state)
                reached.set()
            def poll_feedback(self):
                return None
            def stop(self):
                self.stopped = True

        router = InputRouter(None)
        router._key, router._input = "xinput", Source()
        events, backend = queue.Queue(), Backend()
        engine = Engine(router, events, lambda _: backend)
        engine.start(0, 1)
        if not changed:
            self.assertTrue(reached.wait(2))
            engine.stop()
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertTrue(backend.stopped)
        self.assertEqual(checked, [(0, DEVICE)])  # once, never every frame
        return frames, writes, list(events.queue)

    def test_reused_slot_during_backend_start_never_gets_a_mapped_frame(self):
        frames, writes, events = self.run_startup(True)
        self.assertEqual(frames, [])
        self.assertEqual(writes, [])  # do not stop another pad's game vibration
        self.assertNotIn("status", [kind for kind, _ in events])
        self.assertIn("disconnected", [kind for kind, _ in events])

    def test_original_pad_continues_with_mapping_and_normal_cleanup(self):
        frames, _, events = self.run_startup(False)
        self.assertTrue(frames[0]["touchpad_click"])
        self.assertEqual(frames[0]["buttons"], 0)
        self.assertIn("status", [kind for kind, _ in events])


if __name__ == "__main__":
    unittest.main()
