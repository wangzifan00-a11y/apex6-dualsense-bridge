"""Pure report/edge checks; no physical input, HID handle or bridge is opened."""
import ctypes as C
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dsbridge.diagnostics.touchpad import (AuditFailure, SonyButtons, TouchpadAudit,
                                          _HidReader, _ObservedInput, _Overlapped,
                                          main, parse_sony_input)


NEUTRAL = SonyButtons(False, False, True, True)
PRESSED = SonyButtons(True, False, True, True)


def report(*, pressed=False, create=False, options=False, inactive=True):
    data = bytearray(64)
    data[0] = 1
    data[9] = (0x10 if create else 0) | (0x20 if options else 0)
    data[10] = 2 if pressed else 0
    data[33] = data[37] = 0x80 if inactive else 0
    return bytes(data)


class TouchpadAuditTests(unittest.TestCase):
    def fake_reader(self, payload=None):
        reader = _HidReader.__new__(_HidReader)
        reader.handle, reader.event, reader.pending = 11, 22, False
        reader.buffer = C.create_string_buffer(payload or report(), 64)
        reader.overlapped = _Overlapped()
        reader.api = SimpleNamespace(kernel=Mock())
        reader.api.kernel.ResetEvent.return_value = True
        reader.api.kernel.CloseHandle.return_value = True
        return reader

    def test_native_report_flags_include_report_id_and_ignore_options(self):
        self.assertEqual(parse_sony_input(report(options=True)), NEUTRAL)
        self.assertEqual(parse_sony_input(report(pressed=True, options=True)), PRESSED)
        self.assertTrue(parse_sony_input(report(create=True)).create)
        self.assertFalse(parse_sony_input(report(inactive=False)).touch1_inactive)

    def test_incomplete_wrong_id_and_non_bytes_reports_fail(self):
        for invalid in (b"", bytes(63), bytes(65), bytes(64), bytearray(report()), None):
            with self.subTest(report=invalid):
                with self.assertRaises(AuditFailure) as caught:
                    parse_sony_input(invalid)
                self.assertEqual(caught.exception.code, "invalid_report")

    def test_three_real_press_hold_release_cycles_require_final_release_settle(self):
        audit = TouchpadAudit()
        for cycle in range(3):
            start = 1.0 + cycle * 2
            audit.observe_physical(True, start)
            audit.observe_sony(PRESSED, start + .03)
            self.assertFalse(audit.check(start + .03))
            audit.observe_sony(PRESSED, start + .8)  # Held packets are valid.
            self.assertEqual(audit.snapshot(start + .8)["physical_view"]["presses"], cycle + 1)
            self.assertTrue(audit.snapshot(start + .8)["virtual_touchpad"]["holding_long_enough"])
            audit.observe_physical(False, start + 1)
            audit.observe_sony(NEUTRAL, start + 1.03)
            self.assertFalse(audit.check(start + 1.03))
        self.assertTrue(audit.check(6.7))
        result = audit.snapshot(6.7)
        self.assertEqual(result["matched_edges"], 6)
        self.assertEqual(result["physical_view"]["completed_holds"], 3)
        self.assertEqual(result["virtual_touchpad"]["completed_holds"], 3)
        self.assertFalse(result["create_seen"])
        self.assertTrue(result["contacts_always_inactive"])

    def test_fewer_cycles_or_third_button_still_held_cannot_pass(self):
        audit = TouchpadAudit()
        for cycle in range(2):
            start = 1.0 + cycle * 2
            audit.observe_physical(True, start)
            audit.observe_sony(PRESSED, start + .03)
            audit.observe_physical(False, start + 1)
            audit.observe_sony(NEUTRAL, start + 1.03)
        self.assertFalse(audit.check(4.8))
        audit.observe_physical(True, 5)
        audit.observe_sony(PRESSED, 5.03)
        self.assertFalse(audit.check(20))

    def test_missing_or_unmatched_virtual_edge_fails(self):
        for source in ("physical", "sony"):
            audit = TouchpadAudit()
            if source == "physical":
                audit.observe_physical(True, 1)
            else:
                audit.observe_sony(PRESSED, 1)
            with self.subTest(source=source):
                with self.assertRaises(AuditFailure) as caught:
                    audit.check(1.6)
                self.assertEqual(caught.exception.code, "missing_edge")
        audit = TouchpadAudit()
        audit.observe_physical(True, 1)
        audit.observe_sony(PRESSED, 1.7)
        with self.assertRaises(AuditFailure) as caught:
            audit.check(1.7)
        self.assertEqual(caught.exception.code, "edge_mismatch")

    def test_create_and_active_contacts_fail_immediately(self):
        for buttons, code in ((SonyButtons(True, True, True, True), "create_pressed"),
                              (SonyButtons(True, False, False, True), "touch_active"),
                              (SonyButtons(True, False, True, False), "touch_active")):
            with self.subTest(buttons=buttons):
                with self.assertRaises(AuditFailure) as caught:
                    TouchpadAudit().observe_sony(buttons, 1)
                self.assertEqual(caught.exception.code, code)

    def test_short_hold_short_release_and_extra_press_fail(self):
        audit = TouchpadAudit()
        audit.observe_physical(True, 1)
        with self.assertRaises(AuditFailure) as caught:
            audit.observe_physical(False, 1.2)
        self.assertEqual(caught.exception.code, "short_hold")
        audit = TouchpadAudit()
        audit.observe_physical(True, 1)
        audit.observe_physical(False, 2)
        with self.assertRaises(AuditFailure) as caught:
            audit.observe_physical(True, 2.2)
        self.assertEqual(caught.exception.code, "short_release")
        audit = TouchpadAudit()
        for cycle in range(3):
            audit.observe_physical(True, 1 + cycle * 2)
            audit.observe_physical(False, 2 + cycle * 2)
        with self.assertRaises(AuditFailure) as caught:
            audit.observe_physical(True, 7)
        self.assertEqual(caught.exception.code, "extra_edges")

    def test_proxy_leaves_production_mapping_and_full_input_untouched(self):
        source = Mock(slots=(0, 1))
        source.get_state.return_value = {"buttons": 0, "lx": 100}
        transform = Mock()
        source.prepare_input.return_value = transform
        observed = _ObservedInput(source, 0)
        self.assertIs(observed.get_state(0), source.get_state.return_value)
        self.assertTrue(observed.arm())
        source.get_state.return_value = {"buttons": 0x20, "lx": 100}
        self.assertIs(observed.get_state(0), source.get_state.return_value)
        self.assertEqual(source.get_state.return_value, {"buttons": 0x20, "lx": 100})
        _, value = observed.samples.get_nowait()
        self.assertIs(value, True)
        observed.get_state(0)
        self.assertTrue(observed.samples.empty())
        self.assertIs(observed.prepare_input(0), transform)
        source.prepare_input.assert_called_once_with(0)
        source.validate_input.return_value = False
        self.assertIs(observed.validate_input(0), False)
        source.validate_input.assert_called_once_with(0)

    def test_proxy_refuses_nonzero_rumble_and_permits_only_zero(self):
        source = Mock(slots=(0,))
        observed = _ObservedInput(source, 0)
        with self.assertRaises(AuditFailure):
            observed.set_rumble(0, .1, 0)
        source.set_rumble.assert_not_called()
        observed.set_rumble(0, 0, 0)
        source.set_rumble.assert_called_once_with(0, 0, 0)
        self.assertEqual(observed.nonzero_attempts, 1)

    def test_interruption_before_any_input_can_never_record_passed(self):
        context = Mock(paths=SimpleNamespace(state=Path("unused-test-state")))
        context.create_input.side_effect = KeyboardInterrupt
        saved = []
        with patch("dsbridge.diagnostics.touchpad._save", side_effect=lambda path, value: saved.append(dict(value))):
            with self.assertRaises(KeyboardInterrupt):
                main(context)
        self.assertEqual(saved[-1]["phase"], "failed")
        self.assertFalse(saved[-1]["passed"])
        self.assertFalse(saved[-1]["three_cycles_complete"])
        self.assertEqual(saved[-1]["error_code"], "interrupted")

    def test_immediate_async_read_uses_overlapped_length_and_null_byte_count(self):
        reader = self.fake_reader(report(pressed=True))
        reader.api.kernel.ReadFile.return_value = True
        def complete(handle, overlapped, count, wait):
            count._obj.value = 64
            return True
        reader.api.kernel.GetOverlappedResult.side_effect = complete
        self.assertEqual(parse_sony_input(reader.read()), PRESSED)
        self.assertIsNone(reader.api.kernel.ReadFile.call_args.args[3])
        self.assertFalse(reader.api.kernel.GetOverlappedResult.call_args.args[3])
        self.assertFalse(reader.pending)
        reader.close()

    def test_read_timeout_cancels_and_reaps_before_closing_io_memory(self):
        reader = self.fake_reader()
        calls = []
        kernel = reader.api.kernel
        kernel.ReadFile.return_value = False
        kernel.WaitForSingleObject.side_effect = (258, 0)
        kernel.CancelIoEx.side_effect = lambda *_: calls.append("cancel") or True
        kernel.GetOverlappedResult.side_effect = lambda *_: calls.append("reap") or False
        kernel.CloseHandle.side_effect = lambda *_: calls.append("close") or True
        with patch("dsbridge.diagnostics.touchpad.C.get_last_error", side_effect=(997, 995), create=True):
            self.assertIsNone(reader.read())
        self.assertFalse(reader.pending)
        reader.close()
        self.assertEqual(calls, ["cancel", "reap", "close", "close"])
        self.assertIsNone(reader.handle)
        self.assertIsNone(reader.event)

    def test_cancel_completion_race_preserves_actual_report(self):
        reader = self.fake_reader(report(pressed=True))
        reader.pending = True
        reader.api.kernel.CancelIoEx.return_value = False
        reader.api.kernel.WaitForSingleObject.return_value = 0
        def complete(handle, overlapped, count, wait):
            count._obj.value = 64
            return True
        reader.api.kernel.GetOverlappedResult.side_effect = complete
        with patch("dsbridge.diagnostics.touchpad.C.get_last_error", return_value=1168, create=True):
            self.assertEqual(parse_sony_input(reader._cancel()), PRESSED)
        self.assertFalse(reader.pending)
        reader.close()


if __name__ == "__main__":
    unittest.main()
