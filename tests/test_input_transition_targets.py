"""A restart selection must never escape the chosen controller input branch."""
import copy
import json
import unittest
from unittest.mock import patch

from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.input_transition_targets import (
    BT_HID_SERVICE, HID_CLASS, XUSB_CLASS, InputTargetsError,
    UnsupportedInputTargets, classify_input_targets, resolve_input_targets,
    capture_input_anchor, classify_current_input, resolve_current_input,
)


def node(identifier, parent="", *, children=(), service="", class_guid=HID_CLASS,
         hardware_ids=(), compatible_ids=()):
    return dict(instance_id=identifier, parent=parent, children=list(children),
                service=service, class_guid=class_guid, hardware_ids=list(hardware_ids),
                compatible_ids=list(compatible_ids), present=True)


def apex_nodes(group="00"):
    leaf = rf"HID\VID_37D7&PID_2502&IG_{group}\INPUT"
    hidusb = rf"USB\VID_37D7&PID_2502&IG_{group}\GAME"
    xusb = r"USB\VID_37D7&PID_2502&MI_00\INPUT"
    composite = r"USB\VID_37D7&PID_2502\APEX"
    hub = r"USB\ROOT_HUB30\HUB"
    vendor = r"USB\VID_37D7&PID_2502&MI_03\VENDOR"
    table = {
        leaf: node(leaf, hidusb, hardware_ids=["HID_DEVICE_SYSTEM_GAME", "HID_DEVICE_UP:0001_U:0005"]),
        hidusb: node(hidusb, xusb, children=[leaf], service="HidUsb", compatible_ids=[r"USB\Class_03"]),
        xusb: node(xusb, composite, children=[hidusb], service="xusb22", class_guid=XUSB_CLASS,
                   compatible_ids=[r"USB\MS_COMP_XUSB10"]),
        composite: node(composite, hub, children=[xusb, vendor], service="usbccgp"),
        hub: node(hub, service="USBHUB3"),
    }
    return leaf, table


def bluetooth_nodes():
    leaf = rf"HID\{BT_HID_SERVICE}&VID_045E&PID_02E0&IG_00\INPUT"
    profile = rf"BTHENUM\{BT_HID_SERVICE}_VID&0002045E_PID&02E0\PROFILE"
    enumerator = r"BTH\MS_BTHBRB\ENUMERATOR"
    radio = r"USB\VID_8087&PID_0033\RADIO"
    table = {
        leaf: node(leaf, profile, hardware_ids=["HID_DEVICE_SYSTEM_GAME"]),
        profile: node(profile, enumerator, children=[leaf], service="HidBth",
                      compatible_ids=["BTHENUM\\" + BT_HID_SERVICE]),
        enumerator: node(enumerator, radio, children=[profile, r"BTHENUM\OTHER_DEVICE\PROFILE"], service="BthEnum"),
        radio: node(radio, service="BTHUSB"),
    }
    return leaf, table


class InputTransitionTargetTests(unittest.TestCase):
    def test_group_renumbering_preserves_exact_root_and_physical_ancestry(self):
        old_leaf, old_nodes = apex_nodes()
        original = classify_input_targets(ControllerConnection(kind="receiver", device_id=old_leaf), old_nodes)
        anchor = capture_input_anchor(original, old_nodes)
        new_leaf, new_nodes = apex_nodes("01")
        current = classify_current_input(original, anchor, new_nodes)
        self.assertEqual(current.device_id, new_leaf)
        self.assertEqual(current.restart_id, original.restart_id)
        self.assertEqual(current.transport, "receiver")
        self.assertEqual(capture_input_anchor(current, new_nodes), anchor)

    def test_same_vid_pid_new_physical_path_or_driver_is_never_adopted(self):
        old_leaf, old_nodes = apex_nodes()
        original = classify_input_targets(old_leaf, old_nodes)
        anchor = capture_input_anchor(original, old_nodes)
        _, new_nodes = apex_nodes("01")
        physical = anchor["physical_root_id"]
        for identity, field, value in ((physical, "hardware_ids", ["changed-device"]),
                                       (anchor["hub_id"], "service", "USBHUB"),
                                       (anchor["root_id"], "hardware_ids", ["changed-root"])):
            changed = copy.deepcopy(new_nodes)
            changed[identity][field] = value
            with self.subTest(field=field), self.assertRaises(InputTargetsError):
                classify_current_input(original, anchor, changed)
        changed = copy.deepcopy(new_nodes)
        replacement = physical.replace("APEX", "ANOTHER")
        changed[replacement] = changed.pop(physical)
        changed[replacement]["instance_id"] = replacement
        changed[anchor["root_id"]]["parent"] = replacement
        with self.assertRaises(InputTargetsError):
            classify_current_input(original, anchor, changed)

    def test_reenumeration_must_have_one_complete_game_input(self):
        leaf, nodes = apex_nodes()
        original = classify_input_targets(leaf, nodes)
        anchor = capture_input_anchor(original, nodes)
        for children in ([], ["HID\\OTHER\\ONE", "HID\\OTHER\\TWO"]):
            changed = copy.deepcopy(nodes)
            changed[original.restart_id]["children"] = children
            with self.assertRaises(InputTargetsError):
                classify_current_input(original, anchor, changed)

    def test_current_reader_remains_read_only_and_uses_exact_original_root(self):
        old_leaf, old_nodes = apex_nodes()
        original = classify_input_targets(old_leaf, old_nodes)
        anchor = capture_input_anchor(original, old_nodes)
        new_leaf, new_nodes = apex_nodes("01")
        with patch("dsbridge.platform.windows.input_transition_targets._NativeTree") as api:
            api.return_value.read_current.return_value = new_nodes
            self.assertEqual(resolve_current_input(original, anchor).device_id, new_leaf)
            api.return_value.read_current.assert_called_once_with(original)

    def test_apex_targets_stop_at_game_interface_preserving_vendor_siblings(self):
        for group in ("00", "01", "0a"):
            leaf, table = apex_nodes(group)
            before = copy.deepcopy(table)
            result = classify_input_targets(leaf, table)
            self.assertEqual(result.device_ids, result.hidden_ids)
            self.assertEqual(len(result.device_ids), 3)
            self.assertIn("&MI_00\\", result.restart_id)
            self.assertNotIn(r"USB\VID_37D7&PID_2502\APEX", result.device_ids)
            self.assertEqual(result.transport, "usb")
            self.assertEqual(table, before)
            self.assertIsInstance(json.loads(json.dumps(result.to_dict()))["hidden_ids"], list)

    def test_receiver_or_usb_connection_retains_transport_and_validates_identity(self):
        leaf, table = apex_nodes()
        for kind in ("receiver", "usb"):
            connection = ControllerConnection(kind, device_id=leaf, vendor_id=0x37D7, product_id=0x2502)
            self.assertEqual(classify_input_targets(connection, table).transport, kind)
        for kind, vendor, product in (("bluetooth", 0x37D7, 0x2502), ("usb", 0x045E, 0x2502), ("usb", 0x37D7, 0x1111)):
            with self.assertRaises(InputTargetsError):
                classify_input_targets(ControllerConnection(kind, device_id=leaf, vendor_id=vendor, product_id=product), table)

    def test_ids_are_case_insensitive_and_output_uses_sampled_casing(self):
        leaf, table = apex_nodes()
        result = classify_input_targets(leaf.lower(), table)
        self.assertEqual(result.device_id, leaf)

    def test_bluetooth_stops_at_exclusive_hid_profile_not_radio(self):
        leaf, table = bluetooth_nodes()
        result = classify_input_targets(leaf, table)
        self.assertEqual(result.transport, "bluetooth")
        self.assertEqual(len(result.device_ids), 2)
        self.assertTrue(result.restart_id.startswith("BTHENUM\\"))
        self.assertFalse(any("RADIO" in value or "MS_BTHBRB" in value for value in result.device_ids))

    def test_selected_node_must_be_exact_hid_instance(self):
        leaf, table = apex_nodes()
        for selected in (None, "", "HID\\", r"USB\VID_37D7&PID_2502&MI_00\INPUT",
                         leaf + "\\CHILD", leaf + "*", leaf + "?", leaf + "\0", leaf + "\n"):
            with self.subTest(selected=selected), self.assertRaises(InputTargetsError):
                classify_input_targets(selected, table)

    def test_disconnected_and_virtual_connections_are_rejected(self):
        leaf, table = apex_nodes()
        for kind in ("virtual", "disconnected"):
            with self.assertRaises(InputTargetsError):
                classify_input_targets(ControllerConnection(kind, device_id=leaf), table)
        table[leaf]["present"] = False
        with self.assertRaises(InputTargetsError):
            classify_input_targets(leaf, table)

    def test_apex_must_have_real_verified_driver_stack(self):
        leaf, original = apex_nodes()
        parent = original[leaf]["parent"]
        xusb = original[parent]["parent"]
        composite = original[xusb]["parent"]
        for key, field, bad in ((parent, "service", "HidBth"), (parent, "class_guid", XUSB_CLASS),
                                (xusb, "service", "usbccgp"), (xusb, "class_guid", HID_CLASS),
                                (xusb, "compatible_ids", []), (parent, "compatible_ids", []),
                                (composite, "service", "usbip2_ude")):
            table = copy.deepcopy(original)
            table[key][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_extra_child_at_any_restart_branch_level_is_rejected(self):
        leaf, original = apex_nodes()
        parent = original[leaf]["parent"]
        xusb = original[parent]["parent"]
        for key in (leaf, parent, xusb):
            table = copy.deepcopy(original)
            table[key]["children"].append(r"HID\OTHER\INPUT")
            with self.subTest(key=key), self.assertRaises(UnsupportedInputTargets):
                classify_input_targets(leaf, table)

    def test_missing_or_wrong_child_does_not_pass_an_upward_only_walk(self):
        leaf, original = apex_nodes()
        parent = original[leaf]["parent"]
        for children in ([], [r"HID\OTHER\INPUT"], [leaf, leaf]):
            table = copy.deepcopy(original)
            table[parent]["children"] = children
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_group_mismatch_vendor_interface_and_unrelated_usb_are_rejected(self):
        leaf, table = apex_nodes()
        for replacement in (leaf.replace("IG_00", "IG_01"), leaf.replace("IG_00", "MI_03"), leaf.replace("37D7", "1234")):
            changed = copy.deepcopy(table)
            changed[replacement] = changed.pop(leaf)
            changed[replacement]["instance_id"] = replacement
            changed[changed[replacement]["parent"]]["children"] = [replacement]
            with self.assertRaises(InputTargetsError):
                classify_input_targets(replacement, changed)

    def test_non_game_hid_and_keyboard_mouse_service_are_rejected(self):
        leaf, original = apex_nodes()
        for field, value in (("hardware_ids", ["HID_DEVICE_SYSTEM_KEYBOARD"]),
                             ("service", "kbdhid"), ("service", "mouhid")):
            table = copy.deepcopy(original)
            table[leaf][field] = value
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_virtual_ancestor_anywhere_is_rejected(self):
        leaf, original = apex_nodes()
        for service in ("ViGEmBus", "usbip2_ude", "vjoy", "vhidmini"):
            table = copy.deepcopy(original)
            table[r"USB\ROOT_HUB30\HUB"]["service"] = service
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_missing_cyclic_or_incomplete_tree_is_rejected(self):
        leaf, original = apex_nodes()
        missing = copy.deepcopy(original)
        del missing[r"USB\ROOT_HUB30\HUB"]
        cycle = copy.deepcopy(original)
        cycle[r"USB\ROOT_HUB30\HUB"]["parent"] = leaf
        no_children = copy.deepcopy(original)
        del no_children[leaf]["children"]
        no_props = copy.deepcopy(original)
        del no_props[leaf]["hardware_ids"]
        for table in (missing, cycle, no_children, no_props, {}):
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_bluetooth_profile_must_be_hid_service_and_exclusive(self):
        leaf, original = bluetooth_nodes()
        profile = original[leaf]["parent"]
        for field, value in (("service", "BthEnum"), ("compatible_ids", []),
                             ("children", [leaf, r"HID\OTHER\INPUT"]), ("class_guid", XUSB_CLASS)):
            table = copy.deepcopy(original)
            table[profile][field] = value
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_bluetooth_profile_other_vid_or_ble_is_unsupported(self):
        leaf, original = bluetooth_nodes()
        profile = original[leaf]["parent"]
        for replacement in (profile.replace("045E", "054C"), profile.replace("BTHENUM", "BTHLEDEVICE"),
                             profile.replace("00001124", "0000110B")):
            table = copy.deepcopy(original)
            table[replacement] = table.pop(profile)
            table[replacement]["instance_id"] = replacement
            table[leaf]["parent"] = replacement
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)

    def test_invalid_property_types_and_duplicate_case_nodes_fail_closed(self):
        leaf, original = apex_nodes()
        for field, value in (("children", None), ("service", None), ("hardware_ids", "HID_DEVICE_SYSTEM_GAME")):
            table = copy.deepcopy(original)
            table[leaf][field] = value
            with self.assertRaises(InputTargetsError):
                classify_input_targets(leaf, table)
        table = copy.deepcopy(original)
        table[leaf.lower()] = dict(table[leaf])
        with self.assertRaises(InputTargetsError):
            classify_input_targets(leaf, table)

    def test_native_entry_uses_live_sampling_and_does_not_accept_restart_root(self):
        leaf, table = apex_nodes()
        with patch("dsbridge.platform.windows.input_transition_targets._NativeTree") as native:
            native.return_value.read.return_value = table
            self.assertEqual(resolve_input_targets(leaf), classify_input_targets(leaf, table))
            native.return_value.read.assert_called_once_with(leaf)
            with self.assertRaises(InputTargetsError):
                resolve_input_targets(r"USB\ROOT_HUB30\HUB")
            self.assertEqual(native.return_value.read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
