"""Exercise model selection and the running engine, without native devices."""
from dataclasses import replace
import queue
import struct
import threading
import unittest
from types import SimpleNamespace

from dsbridge.controllers.flydigi.apex6.input_mapping import mapping_for, unchanged
from dsbridge.controllers.registry import InputRouter
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.engine import Engine
from dsbridge.virtual.dualsense.codec import encode_input


class MappingTests(unittest.TestCase):
    def test_only_pro_is_mapped_and_all_transports_preserve_other_controls(self):
        state = dict(buttons=0x20 | 0x10 | 0x1000, left_trigger=73, ly=19001, packet=8)
        for kind in ("bluetooth", "receiver", "usb"):
            connection = ControllerConnection(kind, model="flydigi.apex6pro")
            mapper = mapping_for(connection)
            for pressed in (True, True, False, True, False):
                physical = {**state, "buttons": state["buttons"] if pressed else state["buttons"] & ~0x20}
                saved = dict(physical)
                mapped = mapper(physical)
                bits = struct.unpack_from("<I", encode_input(mapped), 4)[0]
                self.assertEqual(bool(bits & 0x20000), pressed)
                self.assertFalse(bits & 0x1000)  # no simultaneous Create press
                self.assertTrue(bits & 0x2000)   # Options still works
                self.assertTrue(bits & 0x20)     # Cross still works
                self.assertEqual(mapped["left_trigger"], 73)
                self.assertEqual(mapped["ly"], 19001)
                self.assertEqual(physical, saved)
        for model, kind in (("", "bluetooth"), ("flydigi.apex6", "receiver"),
                            ("other", "usb"), ("flydigi.apex6pro", "virtual"),
                            ("flydigi.apex6pro", "unknown")):
            connection = ControllerConnection(kind, model=model)
            self.assertIs(mapping_for(connection), unchanged)
            self.assertEqual(struct.unpack_from("<I", encode_input(mapping_for(connection)(state)), 4)[0] & 0x21000, 0x1000)

    def test_resolver_is_selected_slot_specific_and_extensions_are_untouched(self):
        calls = []
        def describe(slot):
            calls.append(slot)
            return ControllerConnection("bluetooth", device_id="selected")
        router = InputRouter(None, resolve_connection=lambda c: replace(c, model="flydigi.apex6pro"))
        router._key = "xinput"
        router._input = SimpleNamespace(connection=describe)
        self.assertTrue(router.prepare_input(2)({"buttons": 0x20})["touchpad_click"])
        self.assertEqual(calls, [2])
        router._key = "extension.brand"
        self.assertIs(router.prepare_input("custom"), unchanged)
        self.assertEqual(calls, [2])

    def test_engine_applies_mapping_before_backend_and_keeps_physical_ui_state(self):
        for mode in (1, 5):
            frames, order = [], []
            reached = threading.Event()
            physical = {"buttons": 0x20, "left_trigger": 0, "right_trigger": 0}
            class Source:
                def get_state(self, slot):
                    return dict(physical) if slot == 0 else None
                def prepare_input(self, slot):
                    order.append(("prepare", slot))
                    return mapping_for(ControllerConnection("receiver", model="flydigi.apex6pro"))
                def set_rumble(self, *args):
                    return True
                def close(self):
                    pass
            class Backend:
                error = None
                def start(self):
                    order.append(("start", None))
                def update(self, state):
                    frames.append(state)
                    reached.set()
                def set_gain(self, gain):
                    pass
                def poll_feedback(self):
                    return None
                def stop(self):
                    pass
            events = queue.Queue()
            engine = Engine(Source(), events, lambda *_: Backend())
            engine.start(0, mode)
            try:
                self.assertTrue(reached.wait(2))
            finally:
                engine.stop()
                engine.thread.join(2)
            self.assertFalse(engine.running)
            self.assertEqual(order, [("prepare", 0), ("start", None)])
            self.assertTrue(frames[0]["touchpad_click"])
            self.assertEqual(frames[0]["buttons"], 0)
            live = [value for kind, value in list(events.queue) if kind == "live"]
            self.assertTrue(live)
            self.assertEqual(live[0]["state"]["buttons"], 0x20)
            self.assertNotIn("touchpad_click", live[0]["state"])


if __name__ == "__main__":
    unittest.main()
