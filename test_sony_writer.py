"""Mapping/identity checks only; these tests do not open or write any device."""

import ctypes
import unittest
from unittest.mock import patch

from sony_test_writer import (HIDD_ATTRIBUTES, HIDP_CAPS, SonyWriterError,
                              _matching, build_rumble_report, send_rumble)


class SonyWriterTests(unittest.TestCase):
    def test_report_layout_and_explicit_stop(self):
        report = build_rumble_report(96, 48)
        self.assertEqual(len(report), 48)
        self.assertEqual(report[:5], b"\x02\x03\x00\x30\x60")
        self.assertEqual(report[5:], bytes(43))
        self.assertEqual(build_rumble_report(0, 0), b"\x02\x03" + bytes(46))

    def test_not_accepting_unverified_lengths_or_strengths(self):
        for length in (32, 49, 64, 78):
            with self.assertRaises(SonyWriterError):
                build_rumble_report(0, 0, length)
        for value in (-1, 256, 1.5, True):
            with self.assertRaises(ValueError):
                build_rumble_report(value, 0)

    def test_verified_identity_and_usb_report_length(self):
        attrs = HIDD_ATTRIBUTES(ctypes.sizeof(HIDD_ATTRIBUTES), 0x054C, 0x0CE6, 0)
        caps = HIDP_CAPS()
        caps.UsagePage, caps.Usage = 1, 5
        caps.InputReportByteLength, caps.OutputReportByteLength = 64, 48
        self.assertTrue(_matching(attrs, caps))
        caps.OutputReportByteLength = 78
        self.assertFalse(_matching(attrs, caps))
        caps.OutputReportByteLength = 48
        caps.Usage = 4
        self.assertFalse(_matching(attrs, caps))

    def test_explicit_path_required_and_foreign_path_never_written(self):
        with self.assertRaises(SonyWriterError):
            send_rumble("", 0, 0)
        with patch("sony_test_writer.describe_dualsense_outputs", return_value=[]), \
                patch("sony_test_writer._Api") as api:
            with self.assertRaises(SonyWriterError):
                send_rumble("foreign-device", 0, 0)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
