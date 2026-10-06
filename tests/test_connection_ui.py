"""Exercise actual Tk selection callbacks with fake, non-output input adapters."""
from dataclasses import replace
import gc
import json
from pathlib import Path
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from dsbridge.application.context import ApplicationContext
from dsbridge.controllers.registry import ControllerRegistry
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.modes import PROFILES
from dsbridge.core.ports import ControllerAdapter
from dsbridge.dependencies.catalog import Dependency, PACKAGES
from dsbridge.runtime.paths import RuntimePaths
from dsbridge.ui.window import run_gui
from test_modular_architecture import folder, ROOT


class FakeInput:
    slots = (0, 1, 2)
    def get_state(self, slot):
        return dict(buttons=0, left_trigger=0, right_trigger=0, lx=0, ly=0, rx=0, ry=0)
    def set_rumble(self, *args):
        raise AssertionError("selection must not vibrate")
    def close(self):
        pass


class Dependencies:
    packages = PACKAGES
    def check(self):
        return [Dependency(p.key, p.name, "ready", "fake driver") for p in PACKAGES]


def widgets(root):
    for child in root.winfo_children():
        yield child
        yield from widgets(child)


class ConnectionUITests(unittest.TestCase):
    def run_selection(self, slot, connections, *, first_delay=0):
        state = folder()
        registry = ControllerRegistry()
        for key, modes in (("xinput", (PROFILES[1],)), ("flydigi.apex6.receiver", (PROFILES[5],))):
            registry.register(ControllerAdapter(key, key, "Test", ("Test",), ("usb",), ("input",),
                               FakeInput, lambda *a: self.fail("selection created a bridge"),
                               lambda *a: self.fail("selection ran a motor test"), modes))
        context = ApplicationContext(RuntimePaths(ROOT, state), registry=registry, dependencies=Dependencies())
        calls = []
        def detect(mode, index):
            calls.append(index)
            if index == 0 and first_delay:
                time.sleep(first_delay)
            return connections[index]
        context.detect_connection = detect
        real_tk = tk.Tk
        def create():
            root = real_tk()
            def select():
                device = next(w for w in widgets(root) if w.winfo_class() == "TCombobox")
                device.current(slot)
                device.event_generate("<<ComboboxSelected>>")
            root.after(25, select)
            return root
        try:
            with patch("tkinter.Tk", side_effect=create):
                run_gui(smoke=True, context=context)
        finally:
            # Tk callbacks retain cycles after destroy(). Release their Tcl
            # variables here, before a later worker thread triggers cyclic GC.
            gc.collect()
        report = json.loads((state / "gui-smoke.json").read_text(encoding="utf-8"))
        self.assertTrue(report["connection_check_finished"])
        self.assertTrue(report["automatic_modes"])
        self.assertEqual([p["id"] for p in report["mode_selections"]], [1, 5])
        self.assertFalse(any("PS4" in text for text in report["main_controls"]))
        self.assertLessEqual(report["requested_size"][0], 850)
        self.assertLessEqual(report["requested_size"][1], 765)
        self.assertIn(slot, calls)
        return report

    def test_selection_shows_three_transports_and_routes_only_ps5(self):
        values = [ControllerConnection("bluetooth", profile=1), ControllerConnection("receiver", profile=5),
                  ControllerConnection("usb", profile=1)]
        for index, expected in enumerate(values):
            with self.subTest(transport=expected.kind):
                report = self.run_selection(index, values)
                self.assertEqual(report["connection"]["kind"], expected.kind)
                self.assertIn(expected.label, report["connection_text"])
                self.assertEqual(report["selected_profile"], expected.profile)

    def test_slow_previous_result_cannot_overwrite_new_selection(self):
        report = self.run_selection(1, [ControllerConnection("receiver", profile=5),
                                       ControllerConnection("usb", profile=1)], first_delay=.25)
        self.assertEqual(report["connection"]["kind"], "usb")
        self.assertEqual(report["selected_profile"], 1)

    def test_disconnect_during_selection_is_displayed(self):
        report = self.run_selection(1, [ControllerConnection("receiver", profile=5),
                                       ControllerConnection("disconnected")])
        self.assertEqual(report["connection"]["kind"], "disconnected")
        self.assertEqual(report["selected_profile"], 1)


if __name__ == "__main__":
    unittest.main()
