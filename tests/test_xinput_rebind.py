import unittest
from dataclasses import replace
from unittest.mock import patch
from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.xinput import XInput


class RebindTests(unittest.TestCase):
    def setUp(self):
        self.device = ControllerConnection("receiver", device_id="HID\\VID_37D7&PID_2502&IG_00\\chosen", vendor_id=0x37D7, product_id=0x2502)
        self.source = XInput.__new__(XInput)
        self.identity = {"vendor_id": 0x37D7, "product_id": 0x2502}
        self.row = {"instance_id": self.device.device_id, "name": "pad", "ancestors": [
            {"instance_id": self.device.device_id, "service": ""},
            {"instance_id": "USB\\VID_37D7&PID_2502&IG_00\\chosen", "service": "HidUsb"}]}

    def test_same_physical_device_can_move_to_another_slot(self):
        self.source.identity = lambda slot: self.identity if slot == 3 else None
        with patch("dsbridge.platform.windows.connection.present_gamepads", return_value=[self.row]):
            self.assertEqual(self.source.rebind(self.device), 3)

    def test_two_identical_slots_are_not_guessed(self):
        self.source.identity = lambda slot: self.identity if slot in (0, 1) else None
        with patch("dsbridge.platform.windows.connection.present_gamepads") as read:
            self.assertIsNone(self.source.rebind(self.device))
            read.assert_not_called()

    def test_replacement_of_same_vid_pid_is_rejected(self):
        self.source.identity = lambda slot: self.identity if slot == 0 else None
        with patch("dsbridge.platform.windows.connection.present_gamepads", return_value=[{**self.row, "instance_id": self.device.device_id + "OTHER"}]):
            self.assertIsNone(self.source.rebind(self.device))

    def test_two_identical_physical_devices_are_not_guessed(self):
        self.source.identity = lambda slot: self.identity if slot == 0 else None
        with patch("dsbridge.platform.windows.connection.present_gamepads", return_value=[self.row, {**self.row, "instance_id": self.device.device_id + "OTHER"}]):
            self.assertIsNone(self.source.rebind(self.device))

    def test_only_the_validated_current_hid_id_accepts_a_changed_ig_instance(self):
        current = replace(self.device, device_id=r"HID\VID_37D7&PID_2502&IG_01\returned")
        row = {**self.row, "instance_id": current.device_id, "ancestors": [
            {"instance_id": current.device_id, "service": ""},
            {"instance_id": r"USB\VID_37D7&PID_2502&IG_01\returned", "service": "HidUsb"}]}
        self.source.identity = lambda slot: self.identity if slot == 1 else None
        with patch("dsbridge.platform.windows.connection.present_gamepads", return_value=[row]):
            self.assertIsNone(self.source.rebind(self.device))
            self.assertEqual(self.source.rebind(current), 1)

    def test_live_unidentified_slot_prevents_a_false_unique_binding(self):
        self.source.identity = lambda slot: (self.identity if slot == 0 else
                                            {"vendor_id": None, "product_id": None} if slot == 1 else None)
        with patch("dsbridge.platform.windows.connection.present_gamepads") as read:
            self.assertIsNone(self.source.rebind(self.device))
            read.assert_not_called()
