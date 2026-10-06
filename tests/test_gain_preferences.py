"""Preference recovery and real Tk callbacks, with fake input and no motors."""
import gc
import json
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from dsbridge.application.context import ApplicationContext
from dsbridge.controllers.registry import ControllerRegistry
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.engine import Engine
from dsbridge.core.modes import PROFILES
from dsbridge.core.ports import ControllerAdapter
from dsbridge.runtime.paths import RuntimePaths
from dsbridge.runtime.preferences import GainPreferences
from dsbridge.ui.window import run_gui
from test_connection_ui import Dependencies, FakeInput, widgets
from test_modular_architecture import folder, ROOT


class GainPreferenceTests(unittest.TestCase):
    def test_default_and_all_range_boundaries_round_trip_in_user_state(self):
        state = folder()
        prefs = GainPreferences(state)
        self.assertEqual(prefs.percent, 70)
        self.assertIsNone(prefs.load_error)
        self.assertFalse(prefs.path.exists())
        for value in (0, 150, 83.7, 70):
            prefs.save(value)
            self.assertEqual(GainPreferences(state).percent, round(value))
        self.assertEqual(prefs.path.parent, state)

    def test_corrupt_or_invalid_settings_use_default_without_failing_startup(self):
        state = folder()
        path = state / "ui-settings.json"
        values = ["{bad json", "[]", '{"version": 2, "gain_percent": 40}', '{"version": 1}']
        for invalid in (-1, 151, True, "80", None, float("nan"), float("inf"), 10**500):
            values.append(json.dumps({"version": 1, "gain_percent": invalid}))
        for contents in values:
            with self.subTest(contents=contents[:60]):
                path.write_text(contents, encoding="utf-8")
                prefs = GainPreferences(state)
                self.assertEqual(prefs.percent, 70)
                self.assertTrue(prefs.load_error)
                self.assertEqual(path.read_text(encoding="utf-8"), contents)
        path.write_bytes(b"\xff\xfe")
        self.assertEqual(GainPreferences(state).percent, 70)

    def test_failed_atomic_replace_preserves_previous_value_and_can_retry(self):
        state = folder()
        prefs = GainPreferences(state)
        prefs.save(40)
        with patch("dsbridge.runtime.preferences.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                prefs.save(95)
        self.assertEqual(GainPreferences(state).percent, 40)
        self.assertEqual(list(state.glob(".ui-settings-*.tmp")), [])
        self.assertTrue(prefs.save(95))
        self.assertEqual(GainPreferences(state).percent, 95)

    def test_unchanged_percentage_does_not_write_again(self):
        prefs = GainPreferences(folder())
        prefs.save(91)
        with patch("dsbridge.runtime.preferences.os.replace") as replace:
            self.assertFalse(prefs.save(91.1))
            replace.assert_not_called()


class GainUITests(unittest.TestCase):
    def run_window(self, state, action=None, *, allow_debounce=False, diagnostics=None):
        registry = ControllerRegistry()
        for key, mode in (("xinput", 1), ("flydigi.apex6.receiver", 5)):
            registry.register(ControllerAdapter(key, key, "Test", ("Test",), ("usb",), ("input",),
                              FakeInput, lambda *a: self.fail("test created a bridge"),
                              lambda *a: self.fail("test ran motors"), (PROFILES[mode],)))
        context = ApplicationContext(RuntimePaths(ROOT, state), registry=registry,
                                     dependencies=Dependencies(), diagnostics=diagnostics)
        context.detect_connection = lambda *a: ControllerConnection("usb", profile=1)
        engines, seen = [], {}
        def create_engine(*args, **kwargs):
            engine = Engine(*args, **kwargs)
            engines.append(engine)
            return engine
        real_tk = tk.Tk
        def create():
            root = real_tk()
            # Test the real visible-mode close callback, with audio service mocked.
            root.withdraw()
            def exercise():
                try:
                    scale = next(w for w in widgets(root) if w.winfo_class() == "TScale")
                    seen["initial"] = scale.get()
                    if action:
                        action(root, scale, engines[0])
                except Exception as exc:
                    seen["error"] = exc
                def finish():
                    seen["texts"] = [w.cget("text") for w in widgets(root) if "text" in w.keys()]
                    seen["texts"] += [root.getvar(str(w.cget("textvariable"))) for w in widgets(root)
                                      if "textvariable" in w.keys() and str(w.cget("textvariable"))]
                    seen["start_state"] = str(next(w for w in widgets(root)
                        if "text" in w.keys() and w.cget("text") == "启动").cget("state"))
                    seen["gain"] = engines[0].gain
                    root.tk.call(root.protocol("WM_DELETE_WINDOW"))
                root.after(700 if allow_debounce else 20, finish)
            root.after(50, exercise)
            return root
        context.repair_audio = lambda: {"after": {"defaults": {}}, "changes": []}
        try:
            with patch("tkinter.Tk", side_effect=create), patch("dsbridge.ui.window.Engine", side_effect=create_engine), \
                 patch("tkinter.messagebox.showwarning") as warning:
                run_gui(context=context)
        finally:
            # Destroyed Tk widgets and callback closures form reference cycles;
            # their Tcl variables must be released on the creating thread.
            gc.collect()
        if "error" in seen:
            raise seen["error"]
        seen["warnings"] = warning.call_count
        self.assertFalse(any(engine.running for engine in engines))
        return seen

    def test_change_then_close_without_starting_is_restored_on_reopen(self):
        state = folder()
        first = self.run_window(state, lambda root, scale, engine: scale.set(118.4))
        self.assertEqual(first["initial"], 70)
        self.assertEqual(first["gain"], 1.18)
        self.assertEqual(GainPreferences(state).percent, 118)
        reopened = self.run_window(state)
        self.assertEqual(reopened["initial"], 118)
        self.assertEqual(reopened["gain"], 1.18)

    def test_slider_burst_is_saved_once_after_debounce_and_not_again_on_close(self):
        state = folder()
        def drag(root, scale, engine):
            for value in range(80, 101):
                scale.set(value)
        from dsbridge.runtime.preferences import os
        with patch("dsbridge.runtime.preferences.os.replace", wraps=os.replace) as replace:
            self.run_window(state, drag, allow_debounce=True)
            self.assertEqual(replace.call_count, 1)
        self.assertEqual(GainPreferences(state).percent, 100)

    def test_write_failure_is_visible_and_logged_without_interrupting_close(self):
        state = folder()
        GainPreferences(state).save(35)
        diagnostics = Mock()
        with patch("dsbridge.runtime.preferences.os.replace", side_effect=PermissionError("read only")):
            result = self.run_window(state, lambda root, scale, engine: scale.set(100),
                                     allow_debounce=True, diagnostics=diagnostics)
        self.assertTrue(any("强度尚未保存" in str(text) for text in result["texts"]))
        self.assertEqual(result["warnings"], 1)
        self.assertTrue(any(call.args[0] == "gain_settings_save_failed" for call in diagnostics.event.call_args_list))
        self.assertEqual(GainPreferences(state).percent, 35)

    def test_disconnect_event_stays_visible_after_stopped_and_disables_restart(self):
        state = folder()
        def disconnected(root, scale, engine):
            engine.events.put(("disconnected", "手柄连接已断开。"))
            engine.events.put(("stopped", None))
        result = self.run_window(state, disconnected, allow_debounce=True)
        self.assertEqual(result["start_state"], "disabled")
        self.assertTrue(any("重新连接后" in str(text) for text in result["texts"]))
        self.assertIn("输入：已断开    反馈：已停振", result["texts"])
        self.assertEqual(result["warnings"], 0)


if __name__ == "__main__":
    unittest.main()
