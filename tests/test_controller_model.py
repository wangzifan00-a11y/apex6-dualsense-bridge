"""Model recognition is narrower than connection/VID/PID recognition."""
import unittest
from unittest.mock import patch

from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.connection import classify_connection, detect_connection


APEX = {"vendor_id": 0x37D7, "product_id": 0x2502, "flags": 0}
XBOX_BT = {"vendor_id": 0x045E, "product_id": 0x02E0, "flags": 2}


def row(*parents, name="HID-compliant game controller", identifier=r"HID\SELECTED", service=""):
    return {"instance_id": identifier, "name": name,
            "ancestors": [{"instance_id": identifier, "name": name, "service": service}] +
                         [{"instance_id": parent, "name": "", "service": ""} for parent in parents]}


class ModelClassificationTests(unittest.TestCase):
    def test_default_model_preserves_existing_connections(self):
        self.assertEqual(ControllerConnection("usb", profile=1).model, "")
        self.assertEqual(ControllerConnection().to_dict()["model"], "")
        self.assertEqual(ControllerConnection().to_dict()["model_source"], "")

    def test_pro_ids_from_own_usb_or_receiver_query(self):
        selected = row(r"USB\VID_37D7&PID_2502")
        for kind, profile in (("usb", 1), ("receiver", 5)):
            for model in (0x96, 0x98):
                with self.subTest(kind=kind, model=model):
                    value = classify_connection(APEX, [selected], flydigi={"kind": kind, "model": model})
                    self.assertEqual((value.kind, value.profile, value.model), (kind, profile, "flydigi.apex6pro"))
                    self.assertEqual(value.model_source, "protocol")

    def test_base_apex_and_malformed_model_keep_existing_transport_without_pro_policy(self):
        selected = row(r"USB\VID_37D7&PID_2502", name="Flydigi APEX 6 Pro")
        for model in (0x95, 0, None, "152", 152.0, True):
            value = classify_connection(APEX, [selected], flydigi={"kind": "receiver", "model": model})
            self.assertEqual((value.kind, value.profile, value.model), ("receiver", 5, ""))
            self.assertEqual(value.model_source, "")

    def test_shared_xbox_bluetooth_identity_is_never_guessed(self):
        selected = row(r"BTHENUM\DEV_AABBCCDDEEFF", name="Xbox Wireless Controller")
        value = classify_connection(XBOX_BT, [selected])
        self.assertEqual((value.kind, value.model), ("bluetooth", ""))
        self.assertEqual(value.model_source, "")

    def test_explicit_english_and_chinese_bluetooth_names(self):
        for name in ("Flydigi APEX 6 Pro", "APEX6 Pro", "Flydigi APEX6 Pro Phantom Blade Zero",
                     "飞智八爪鱼6Pro", "八爪鱼 6 Pro", "八爪鱼6Pro影之刃零"):
            with self.subTest(name=name):
                value = classify_connection(XBOX_BT, [row(r"BTHENUM\DEV_AABBCCDDEEFF", name=name)])
                self.assertEqual(value.model, "flydigi.apex6pro")
                self.assertEqual(value.model_source, "device_name")

    def test_bluetooth_own_parent_name_is_usable_but_usb_radio_name_is_not(self):
        selected = row(r"BTHENUM\DEV_AABBCCDDEEFF", r"USB\VID_8087&PID_0033")
        selected["ancestors"][1]["name"] = "Flydigi APEX6 Pro"
        self.assertEqual(classify_connection(XBOX_BT, [selected]).model, "flydigi.apex6pro")
        selected["ancestors"][1]["name"] = "Xbox Wireless Controller"
        selected["ancestors"][2]["name"] = "Flydigi APEX6 Pro"
        self.assertEqual(classify_connection(XBOX_BT, [selected]).model, "")

    def test_bluetooth_names_must_identify_the_pro_model_explicitly(self):
        for name in ("APEX6", "APEX 6 Professional", "APEX 6 Pro2", "Xbox Wireless Controller (APEX6 Pro)"):
            self.assertEqual(classify_connection(XBOX_BT, [row(r"BTHENUM\PAD", name=name)]).model, "")

    def test_non_flydigi_usb_cannot_borrow_vendor_reply(self):
        value = classify_connection(XBOX_BT, [row(r"USB\VID_045E&PID_02E0")],
                                    flydigi={"kind": "receiver", "model": 0x98})
        self.assertEqual((value.kind, value.model), ("receiver", ""))


class ModelQueryGuardTests(unittest.TestCase):
    def detect(self, rows, reply=None):
        with patch("dsbridge.platform.windows.connection.present_gamepads", return_value=rows), \
                patch("dsbridge.controllers.flydigi.apex6.connection.query_connection", return_value=reply) as query:
            result = detect_connection(APEX)
        return result, query

    def test_virtual_vid_match_does_not_query_a_physical_controller(self):
        value, query = self.detect([row(r"USB\VID_37D7&PID_2502", service="usbip2_ude")])
        self.assertEqual((value.kind, value.model), ("virtual", ""))
        query.assert_not_called()

    def test_ambiguous_or_missing_inputs_do_not_query(self):
        for rows in ([], [row(r"USB\VID_37D7&PID_2502", identifier=r"HID\A"),
                          row(r"USB\VID_37D7&PID_2502", identifier=r"HID\B")]):
            value, query = self.detect(rows)
            self.assertEqual((value.kind, value.model), ("unknown", ""))
            query.assert_not_called()

    def test_bluetooth_and_unknown_nonusb_inputs_do_not_query(self):
        for selected in (row(r"BTHENUM\PAD"), row(r"PCI\NONUSB")):
            _, query = self.detect([selected])
            query.assert_not_called()

    def test_unique_usb_queries_selected_row_and_preserves_pro_model(self):
        selected = row(r"USB\VID_37D7&PID_2502")
        value, query = self.detect([selected], {"kind": "usb", "model": 0x98})
        self.assertEqual((value.kind, value.profile, value.model), ("usb", 1, "flydigi.apex6pro"))
        query.assert_called_once_with(selected)

    def test_duplicate_same_node_is_unique_after_deduplication(self):
        selected = row(r"USB\VID_37D7&PID_2502")
        value, query = self.detect([selected, dict(selected)], {"kind": "receiver", "model": 0x96})
        self.assertEqual((value.kind, value.model), ("receiver", "flydigi.apex6pro"))
        query.assert_called_once_with(selected)


if __name__ == "__main__":
    unittest.main()
