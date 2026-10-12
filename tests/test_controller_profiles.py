"""Model bindings and actual Tk controls, without native devices or vibration."""
from dataclasses import replace
import gc
import hashlib
import json
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from dsbridge.application.context import ApplicationContext
from dsbridge.controllers.registry import ControllerRegistry
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.modes import PROFILES
from dsbridge.core.ports import ControllerAdapter
from dsbridge.runtime.controller_profiles import APEX6PRO_MODEL, ControllerProfiles
from dsbridge.runtime.paths import RuntimePaths
from dsbridge.ui.window import run_gui
from test_connection_ui import Dependencies, FakeInput, widgets
from test_modular_architecture import folder, ROOT


def bluetooth(device_id="Test-Bluetooth-Device-A"):
    return ControllerConnection("bluetooth", device_id=device_id, vendor_id=0x045e,
                                product_id=0x02e0, profile=1)


class ControllerProfileTests(unittest.TestCase):
    def test_xbox_bluetooth_identity_is_not_a_model_guess(self):
        connection = bluetooth()
        store = ControllerProfiles(folder())
        self.assertIs(store.resolve(connection), connection)
        self.assertFalse(store.path.exists())

    def test_saved_binding_uses_casefold_hash_and_only_affects_that_device(self):
        state = folder()
        connection = bluetooth()
        store = ControllerProfiles(state)
        self.assertEqual(store.set_bluetooth_pro(connection, True).model, APEX6PRO_MODEL)
        contents = store.path.read_text(encoding="utf-8")
        self.assertNotIn(connection.device_id, contents)
        key = hashlib.sha256(connection.device_id.casefold().encode("utf-8")).hexdigest()
        self.assertEqual(json.loads(contents), {"version": 1, "bluetooth": {key: APEX6PRO_MODEL}})
        reopened = ControllerProfiles(state)
        self.assertEqual(reopened.resolve(replace(connection, device_id=connection.device_id.upper())).model,
                         APEX6PRO_MODEL)
        self.assertEqual(reopened.resolve(connection).model_source, "user")
        self.assertEqual(reopened.resolve(bluetooth("Test-Bluetooth-Device-B")).model, "")
        self.assertEqual(reopened.resolve(replace(connection, kind="usb")).model, "")

    def test_unbinding_preserves_other_device_and_native_identification(self):
        state = folder()
        store = ControllerProfiles(state)
        first, other = bluetooth(), bluetooth("another")
        selected = store.set_bluetooth_pro(first, True)
        store.set_bluetooth_pro(other, True)
        cleared = store.set_bluetooth_pro(selected, False)
        self.assertEqual((cleared.model, cleared.model_source), ("", ""))
        self.assertEqual(store.resolve(selected).model, "")
        reopened = ControllerProfiles(state)
        self.assertEqual(reopened.resolve(first).model, "")
        self.assertEqual(reopened.resolve(other).model, APEX6PRO_MODEL)
        native = ControllerConnection("receiver", model=APEX6PRO_MODEL)
        self.assertIs(reopened.resolve(native), native)

    def test_native_bluetooth_model_wins_over_binding_and_cannot_be_unchecked(self):
        store = ControllerProfiles(folder())
        original = bluetooth()
        store.set_bluetooth_pro(original, True)
        before = store.path.read_bytes()
        for model, source in ((APEX6PRO_MODEL, "device_name"), (APEX6PRO_MODEL, ""),
                               ("other.identified.model", "device_name")):
            native = replace(original, model=model, model_source=source)
            with self.subTest(source=source, model=model):
                self.assertIs(store.resolve(native), native)
                with self.assertRaises(ValueError):
                    store.set_bluetooth_pro(native, False)
        self.assertEqual(store.path.read_bytes(), before)

    def test_invalid_or_unidentified_device_cannot_be_bound(self):
        store = ControllerProfiles(folder())
        for connection in (bluetooth(""), bluetooth(" "), ControllerConnection("unknown"),
                           ControllerConnection("usb", device_id="test")):
            with self.subTest(connection=connection):
                with self.assertRaises(ValueError):
                    store.set_bluetooth_pro(connection, True)
        self.assertFalse(store.path.exists())

    def test_corruption_does_not_enable_mapping_and_explicit_save_repairs_settings(self):
        state = folder()
        for contents in ("{", "[]", '{"version":true,"bluetooth":{}}',
                         '{"version":1,"bluetooth":{"raw-id":"flydigi.apex6pro"}}'):
            (state / "controller-profiles.json").write_text(contents, encoding="utf-8")
            store = ControllerProfiles(state)
            self.assertTrue(store.load_error)
            self.assertEqual(store.resolve(bluetooth()).model, "")
            store.set_bluetooth_pro(bluetooth(), True)
            self.assertIsNone(ControllerProfiles(state).load_error)

    def test_failed_write_does_not_activate_or_remove_mapping_and_can_retry(self):
        state = folder()
        store = ControllerProfiles(state)
        first, second = bluetooth(), bluetooth("second")
        store.set_bluetooth_pro(first, True)
        before = store.path.read_bytes()
        with patch("dsbridge.runtime.controller_profiles.os.replace", side_effect=PermissionError("locked")):
            for connection, enabled in ((second, True), (first, False)):
                with self.assertRaises(PermissionError):
                    store.set_bluetooth_pro(connection, enabled)
        self.assertEqual(store.resolve(first).model, APEX6PRO_MODEL)
        self.assertEqual(store.resolve(second).model, "")
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(list(state.glob(".controller-profiles-*.tmp")), [])
        self.assertEqual(store.set_bluetooth_pro(second, True).model, APEX6PRO_MODEL)


class ControllerProfileUITests(unittest.TestCase):
    def make_context(self, state, connection):
        class Input(FakeInput):
            slots = (0,)
            def connection(self, _slot):
                return connection
        registry = ControllerRegistry()
        for key, mode in (("xinput", 1), ("flydigi.apex6.receiver", 5)):
            registry.register(ControllerAdapter(key, key, "Test", ("Test",), ("usb",), ("input",),
                              Input, lambda *a: self.fail("created a bridge"),
                              lambda *a: self.fail("sent motor output"), (PROFILES[mode],)))
        return ApplicationContext(RuntimePaths(ROOT, state), registry=registry,
                                  dependencies=Dependencies(), diagnostics=Mock())

    def run_ui(self, connection, *, state=None, toggle=False, fail_save=False, busy_test=False):
        state = state or folder()
        context = self.make_context(state, connection)
        original_detect = context.detect_connection
        def delayed_detect(*args):
            time.sleep(.03)
            return original_detect(*args)
        context.detect_connection = delayed_detect
        observed = {}
        real_tk = tk.Tk
        def create():
            root = real_tk()
            def ready():
                check = next(w for w in widgets(root) if w.winfo_class() == "TCheckbutton")
                if "detect_disabled" not in observed:
                    observed["detect_disabled"] = str(check.cget("state")) == "disabled"
                if connection.kind == "bluetooth" and connection.device_id and str(check.cget("state")) == "disabled":
                    root.after(15, ready)
                    return
                if toggle:
                    check.invoke()
                if busy_test:
                    button = next(w for w in widgets(root) if "text" in w.keys() and w.cget("text") == "测试左右震动")
                    button.invoke()
                    def busy_state():
                        observed["busy_disabled"] = str(check.cget("state")) == "disabled"
                    root.after(120, busy_state)
            root.after(10, ready)
            return root
        if fail_save:
            context.set_bluetooth_pro = Mock(side_effect=PermissionError("read only"))
        if busy_test:
            context.test_motors = lambda *a: time.sleep(.2)
        try:
            with patch("tkinter.Tk", side_effect=create):
                run_gui(smoke=True, context=context)
        finally:
            gc.collect()
        report = json.loads((state / "gui-smoke.json").read_text(encoding="utf-8"))
        self.assertEqual(report["gui"], "ok")
        self.assertLessEqual(report["requested_size"][0], 850)
        self.assertLessEqual(report["requested_size"][1], 765)
        return context, report, observed, state

    def test_context_resolves_persisted_binding_for_detection_and_input_preparation(self):
        state = folder()
        context = self.make_context(state, bluetooth())
        context.set_bluetooth_pro(bluetooth(), True)
        self.assertEqual(context.detect_connection(1, 0).model, APEX6PRO_MODEL)
        port = context.create_input(1)
        try:
            self.assertEqual(port.resolve_connection(bluetooth()).model, APEX6PRO_MODEL)
        finally:
            port.close()

    def test_bluetooth_confirmation_persists_and_is_reversible_without_starting(self):
        _, enabled, observed, state = self.run_ui(bluetooth(), toggle=True)
        self.assertTrue(observed["detect_disabled"])
        self.assertTrue(enabled["controller_profile_checked"])
        self.assertEqual(enabled["connection"]["model"], APEX6PRO_MODEL)
        self.assertEqual(enabled["connection"]["model_source"], "user")
        _, reopened, _, _ = self.run_ui(bluetooth(), state=state)
        self.assertTrue(reopened["controller_profile_checked"])
        _, disabled, _, _ = self.run_ui(bluetooth(), state=state, toggle=True)
        self.assertFalse(disabled["controller_profile_checked"])
        self.assertEqual(ControllerProfiles(state).resolve(bluetooth()).model, "")

    def test_native_pro_is_checked_but_no_user_override_or_other_pad_change(self):
        for connection, checked in ((ControllerConnection("receiver", model=APEX6PRO_MODEL, profile=5), True),
                                     (ControllerConnection("usb", model=APEX6PRO_MODEL, profile=1), True),
                                     (replace(bluetooth(), model=APEX6PRO_MODEL, model_source="device_name"), True),
                                     (replace(bluetooth(), model=APEX6PRO_MODEL), True),
                                     (ControllerConnection("usb", profile=1), False), (bluetooth(""), False)):
            with self.subTest(connection=connection):
                _, report, _, state = self.run_ui(connection)
                self.assertEqual(report["controller_profile_checked"], checked)
                self.assertEqual(report["controller_profile_state"], "disabled")
                self.assertFalse((state / "controller-profiles.json").exists())

    def test_save_failure_reverts_checkbox_and_records_visible_failure(self):
        context, report, _, state = self.run_ui(bluetooth(), toggle=True, fail_save=True)
        self.assertFalse(report["controller_profile_checked"])
        self.assertEqual(report["connection"]["model"], "")
        self.assertFalse((state / "controller-profiles.json").exists())
        self.assertTrue(any(c.args[0] == "controller_profile_save_failed" for c in context.diagnostics.event.call_args_list))

    def test_motor_test_busy_state_disables_binding(self):
        _, _, observed, _ = self.run_ui(bluetooth(), busy_test=True)
        self.assertTrue(observed["busy_disabled"])


if __name__ == "__main__":
    unittest.main()
