"""Pure input-codec checks; no controller, driver or network is opened."""

import struct
import unittest

from dsbridge.virtual.dualsense.codec import encode_input


def sony_buttons(payload):
    return struct.unpack_from("<I", payload, 4)[0]


class TouchpadCodecTests(unittest.TestCase):
    def test_press_hold_and_release_preserve_button_level(self):
        states = ({"touchpad_click": True}, {"touchpad_click": True},
                  {"touchpad_click": False}, {})
        self.assertEqual([sony_buttons(encode_input(state)) for state in states],
                         [0x20000, 0x20000, 0, 0])

    def test_options_create_and_other_inputs_are_preserved(self):
        state = {"buttons": 0x0010 | 0x0020 | 0x1000 | 0x0100 | 0x0001,
                 "left_trigger": 17, "right_trigger": 31,
                 "lx": -1234, "ly": 5678, "rx": 12345, "ry": -12345}
        before = dict(state)
        ordinary = encode_input(state)
        pressed = encode_input({**state, "touchpad_click": True})
        self.assertEqual(sony_buttons(ordinary), 0x2000 | 0x1000 | 0x20 | 0x100 | 0x400 | 0x800)
        self.assertEqual(sony_buttons(pressed), sony_buttons(ordinary) | 0x20000)
        self.assertEqual(pressed[:4], ordinary[:4])
        self.assertEqual(pressed[8:], ordinary[8:])
        self.assertEqual(state, before)

    def test_touchpad_click_does_not_synthesize_touch_contacts(self):
        payload = encode_input({"touchpad_click": True})
        self.assertEqual(len(payload), 33)
        self.assertEqual(payload[11:16], b"\0\0\0\0\x80")
        self.assertEqual(payload[16:21], b"\0\0\0\0\x80")

    def test_non_boolean_click_values_fail_closed(self):
        for value in (0, 1, -1, 1.0, None, "true", "", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "touchpad_click"):
                    encode_input({"touchpad_click": value})


if __name__ == "__main__":
    unittest.main()
