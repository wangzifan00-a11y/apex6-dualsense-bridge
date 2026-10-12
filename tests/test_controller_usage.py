"""Handle evidence must remain exact, read-only and conservative."""
import base64
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.controller_usage import (
    _decode_request, _last_report, _result, match_input_handle,
    probe_controller_usage, selected_input_nodes,
)


def node(identity, service="", pdo=r"\Device\Input"):
    return {"instance_id": identity, "service": service, "pdo": pdo}


class InputScopeTests(unittest.TestCase):
    def test_exact_selected_usb_input_and_xinput_parents_only(self):
        identity = r"HID\VID_37D7&PID_2502&IG_00\selected"
        chain = [node(identity), node(r"USB\VID_37D7&PID_2502&IG_00\selected", "HidUsb", r"\Device\HidParent"),
                 node(r"USB\VID_37D7&PID_2502&MI_00\selected", "xusb22", r"\Device\XusbParent"),
                 node(r"USB\VID_37D7&PID_2502\composite", "usbccgp", r"\Device\Composite"),
                 node(r"USB\ROOT_HUB30\hub", "USBHUB3", r"\Device\Hub")]
        self.assertEqual(selected_input_nodes(identity.lower(), chain), tuple(chain[:3]))

    def test_bluetooth_never_reaches_radio_or_usb_ancestors(self):
        identity = r"HID\BLUETOOTH_PAD\selected"
        chain = [node(identity), node(r"BTHENUM\SERVICE\selected", "HidBth", r"\Device\BthPad"),
                 node(r"BTHENUM\DEV_123\radio", "BthEnum", r"\Device\Radio"),
                 node(r"USB\VID_1234&PID_1234\radio", "HidUsb", r"\Device\UsbRadio")]
        self.assertEqual(selected_input_nodes(identity, chain), tuple(chain[:2]))

    def test_no_vid_matching_ambiguity_or_virtual_fallback(self):
        self.assertEqual(selected_input_nodes(r"HID\A", [node(r"HID\B")]), ())
        self.assertEqual(selected_input_nodes(r"HID\A", [node(r"HID\A", "usbip2_ude")]), ())
        self.assertEqual(selected_input_nodes(r"HID\A", [node(r"HID\A"),
                         node(r"USB\COMPOSITE", "usbccgp"), node(r"ROOT\VIRTUAL", "usbip2_ude")]), ())
        self.assertEqual(selected_input_nodes(r"USB\A", [node(r"USB\A")]), ())
        self.assertEqual(selected_input_nodes(r"HID\A", [node(r"HID\A", pdo="")]), ())


class EvidenceTests(unittest.TestCase):
    targets = {r"\device\selected": "selected_hid"}
    record = {"pid": 42, "access": 0x120089, "type": "File", "name": "consumer.exe",
              "object_name": r"\Device\Selected"}

    def test_exact_input_handle_is_evidence_not_executable_or_dll_guess(self):
        self.assertEqual(match_input_handle(self.record, self.targets),
                         {"pid": 42, "name": "consumer.exe", "source": "selected_hid", "evidence": "open_input_handle"})
        for wrong in (r"\Device\Selected2", r"\Device\Selected\other", r"\Device\Other"):
            self.assertIsNone(match_input_handle({**self.record, "object_name": wrong,
                                                  "name": "Cyberpunk2077.exe", "dll": "xinput1_4.dll"}, self.targets))

    def test_self_system_metadata_and_output_only_handles_are_not_inputs(self):
        for change in ({"pid": 4}, {"pid": True}, {"pid": "42"}, {"type": "Event"},
                       {"access": 0x120080}, {"access": 0x120116}):
            self.assertIsNone(match_input_handle({**self.record, **change}, self.targets))
        self.assertIsNone(match_input_handle(self.record, self.targets, exclude_pids=[42]))

    def test_empty_scan_never_claims_no_game_uses_device(self):
        for done in (True, False):
            report = _result(scan_complete=done)
            self.assertEqual(report["state"], "unknown")
            self.assertFalse(report["complete"])
            self.assertEqual(report["handle_scan_complete"], done)
            self.assertIn("raw_input_registration_is_process_private", report["reasons"])

    def test_held_evidence_survives_incomplete_access_but_never_claims_game_use(self):
        client = match_input_handle(self.record, self.targets)
        report = _result([client, client], reasons=["process_access_denied_or_exited"])
        self.assertEqual(report["state"], "held")
        self.assertEqual(report["clients"], [client])
        self.assertFalse(report["complete"])
        self.assertNotIn("game", report["clients"][0])

    def test_request_rejects_wrong_identity_type_and_boolean_pid(self):
        for data in ({}, {"device_id": 1}, {"device_id": "HID\\A", "exclude_pids": [True]},
                     {"device_id": "HID\\A", "exclude_pids": "42"}):
            token = base64.b64encode(json.dumps(data).encode()).decode()
            with self.assertRaises(ValueError):
                _decode_request(token)

    def test_partial_output_parser_uses_only_last_valid_report(self):
        report = _result([match_input_handle(self.record, self.targets)])
        output = b"not json\n" + json.dumps(report).encode() + b"\n{truncated"
        self.assertEqual(_last_report(output), report)


class BoundedProcessTests(unittest.TestCase):
    connection = ControllerConnection("usb", device_id=r"HID\SELECTED")

    def test_timeout_kills_only_the_owned_probe_and_keeps_partial_evidence(self):
        held = _result([{"pid": 77, "name": "consumer.exe", "source": "selected_hid"}])
        child = MagicMock(returncode=-1)
        child.communicate.side_effect = [subprocess.TimeoutExpired("owned probe", 1),
                                         (json.dumps(held).encode(), None)]
        with patch("dsbridge.platform.windows.controller_usage.subprocess.Popen", return_value=child):
            result = probe_controller_usage(self.connection, timeout=1)
        child.kill.assert_called_once_with()
        self.assertEqual(result["state"], "held")
        self.assertIn("probe_timeout", result["reasons"])
        self.assertFalse(result["complete"])

    def test_unresolved_virtual_and_failed_launch_cannot_be_reported_idle(self):
        with patch("dsbridge.platform.windows.controller_usage.subprocess.Popen") as launch:
            self.assertEqual(probe_controller_usage(ControllerConnection("virtual", device_id="HID\\A"))["state"], "unknown")
            self.assertEqual(probe_controller_usage(ControllerConnection("usb"))["state"], "unknown")
            launch.assert_not_called()
        with patch("dsbridge.platform.windows.controller_usage.subprocess.Popen", side_effect=OSError("denied")):
            report = probe_controller_usage(self.connection)
        self.assertEqual(report["state"], "unknown")
        self.assertTrue(any(reason.startswith("probe_launch_failed") for reason in report["reasons"]))

    def test_worker_request_excludes_caller_and_preserves_exact_identity(self):
        child = MagicMock(returncode=0)
        child.communicate.return_value = (json.dumps(_result(scan_complete=True)).encode(), None)
        with patch("dsbridge.platform.windows.controller_usage.subprocess.Popen", return_value=child) as launch:
            report = probe_controller_usage(self.connection, exclude_pids=[123])
        request = _decode_request(launch.call_args.args[0][-1])
        self.assertEqual(request["device_id"], self.connection.device_id)
        self.assertIn(123, request["exclude_pids"])
        self.assertEqual(report["state"], "unknown")
        child.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
