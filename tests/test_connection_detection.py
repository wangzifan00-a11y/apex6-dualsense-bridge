"""Selection binding, transport precedence, unplugging, and PS5-only routing."""
from dataclasses import replace
import unittest
from unittest.mock import Mock
from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.connection import classify_connection
from dsbridge.controllers.flydigi.apex6.connection import connection_kind
from dsbridge.application.context import ApplicationContext
from dsbridge.core.modes import PROFILES


def node(*parents, service="", identifier="HID\\SELECTED", name="Gamepad"):
    return {"instance_id": identifier, "name": name,
            "ancestors": [{"instance_id": identifier, "service": service, "name": name}] +
                         [{"instance_id": p, "service": "", "name": ""} for p in parents]}


GENERIC = {"vendor_id": 0x045E, "product_id": 0x028E, "flags": 0}
APEX = {"vendor_id": 0x37D7, "product_id": 0x2502, "flags": 0}


class ConnectionTests(unittest.TestCase):
    def test_bluetooth_radio_usb_parent_does_not_make_the_pad_usb(self):
        row = node("BTHENUM\\GAMEPAD", "USB\\VID_8087&PID_0033")
        result = classify_connection(GENERIC, [row])
        self.assertEqual((result.kind, result.profile), ("bluetooth", 1))

    def test_bluetooth_le_is_recognized(self):
        result = classify_connection(GENERIC, [node("BTHLEDEVICE\\GAMEPAD")])
        self.assertEqual(result.kind, "bluetooth")

    def test_usb_wired_gamepad(self):
        result = classify_connection(GENERIC, [node("USB\\VID_045E&PID_028E")])
        self.assertEqual((result.kind, result.profile), ("usb", 1))

    def test_wireless_flag_on_usb_is_receiver(self):
        result = classify_connection(dict(GENERIC, flags=2), [node("USB\\VID_045E&PID_028E")])
        self.assertEqual(result.kind, "receiver")

    def test_apex_receiver_requires_own_protocol_not_its_usb_plug(self):
        row = node("USB\\VID_37D7&PID_2502")
        for kind, profile in (("receiver", 5), ("usb", 1)):
            with self.subTest(kind=kind):
                result = classify_connection(APEX, [row], flydigi={"kind": kind})
                self.assertEqual((result.kind, result.profile), (kind, profile))
        self.assertEqual(classify_connection(APEX, [row]).kind, "unknown")

    def test_protocol_architectures_have_different_connection_values(self):
        self.assertEqual(connection_kind(True, 0), "usb")
        self.assertEqual(connection_kind(True, 1), "receiver")
        self.assertEqual(connection_kind(False, 1), "usb")
        self.assertEqual(connection_kind(False, 2), "receiver")
        self.assertEqual(connection_kind(True, 2), "unknown")

    def test_identical_pads_with_different_transports_are_not_guessed(self):
        rows = [node("BTHENUM\\PAD", identifier="HID\\A"),
                node("USB\\VID_045E&PID_028E", identifier="HID\\B")]
        result = classify_connection(GENERIC, rows)
        self.assertEqual(result.kind, "unknown")
        self.assertIn("唯一对应", result.detail)

    def test_missing_identity_never_reuses_another_device(self):
        self.assertEqual(classify_connection(None, [node("USB\\VID_OTHER")]).kind, "disconnected")
        self.assertEqual(classify_connection({"vendor_id": None, "product_id": None}, []).kind, "unknown")

    def test_stale_absent_pnp_nodes_are_not_assumed_bluetooth(self):
        self.assertEqual(classify_connection(GENERIC, []).kind, "unknown")

    def test_virtual_usb_is_not_a_physical_wired_pad(self):
        row = node("USB\\VID_054C&PID_0CE6", service="usbip2_ude")
        self.assertEqual(classify_connection(GENERIC, [row]).kind, "virtual")

    def test_selected_adapter_worker_is_closed_even_on_native_failure(self):
        source = Mock()
        source.connection.side_effect = RuntimeError("query failed")
        context = ApplicationContext.__new__(ApplicationContext)
        context.registry = Mock(profiles=PROFILES, adapters={"xinput": Mock(input_factory=lambda: source)})
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            context.detect_connection(1, 2)
        source.get_state.assert_called_once_with(2)
        source.connection.assert_called_once_with(2)
        source.close.assert_called_once()

    def test_replacing_receiver_by_usb_resets_automatic_profile(self):
        context = ApplicationContext.__new__(ApplicationContext)
        context.registry = Mock(profiles=PROFILES)
        self.assertEqual(context.connection_profile(1, ControllerConnection("receiver", profile=5)), 5)
        self.assertEqual(context.connection_profile(5, ControllerConnection("usb", profile=1)), 1)
        self.assertEqual(context.connection_profile(5, ControllerConnection()), 1)
        self.assertEqual(context.connection_profile("extension.ps5", ControllerConnection()), "extension.ps5")

    def test_restoration_accepts_reenumerated_input_group_but_not_vendor_hid(self):
        from dsbridge.controllers.flydigi.apex6.recovery import TARGET
        self.assertIsNotNone(TARGET.fullmatch(r"HID\VID_37D7&PID_2502&IG_01\8&abc&0&0000"))
        self.assertIsNone(TARGET.fullmatch(r"HID\VID_37D7&PID_2502&MI_01\8&abc&0&0000"))
        self.assertIsNone(TARGET.fullmatch(r"USB\VID_37D7&PID_2502\SERIAL"))


if __name__ == "__main__":
    unittest.main()
