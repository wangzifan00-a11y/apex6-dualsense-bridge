"""Client protocol, identity gates and cancellation without changing devices."""
import base64
import ctypes as C
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from dsbridge.platform.windows.hot_switch_service import (
    API_VERSION, HELPER_VERSION, PIPE_NAME, HotSwitchClient, HotSwitchError,
    _Native, _reply, _status_reply, _wire_request, _worker_request, helper_artifact, worker_main,
)
from dsbridge.runtime.paths import RuntimePaths


SID = "S-1-5-21-100-200-300-1001"
DEVICE = r"HID\VID_37D7&PID_2502&IG_00\selected"


def status(**changes):
    return {"ok": True, "api_version": API_VERSION, "version": HELPER_VERSION,
            "permitted_owner": SID, "restart_required": False, **changes}


class ProtocolTests(unittest.TestCase):
    def test_status_has_no_device_and_restart_has_only_one_exact_hid(self):
        self.assertEqual(_wire_request("status"), {"version": 1, "operation": "status"})
        self.assertEqual(_wire_request("restart", DEVICE),
                         {"version": 1, "operation": "restart", "device_id": DEVICE})
        for invalid in ("", None, "HID\\*", "USB\\VID_37D7\\selected", DEVICE + "\n", DEVICE + "\\more"):
            with self.subTest(invalid=invalid), self.assertRaises(HotSwitchError):
                _wire_request("restart", invalid)
        with self.assertRaises(HotSwitchError):
            _wire_request("status", DEVICE)

    def test_worker_rejects_unknown_fields_operations_boolean_versions(self):
        for request in ({"version": True, "operation": "status"}, {"version": 2, "operation": "status"},
                        {"version": 1, "operation": "install"}, {"version": 1, "operation": "status", "path": "x"},
                        {"version": 1, "operation": "restart", "device_id": "HID\\*"}):
            token = base64.b64encode(json.dumps(request).encode()).decode()
            with self.subTest(request=request), self.assertRaises(HotSwitchError):
                _worker_request(token)

    def test_status_requires_exact_version_current_user_and_no_reboot(self):
        self.assertEqual(_status_reply(status(), SID), status())
        for changes, code in (({"api_version": True}, "version_mismatch"), ({"version": "0.9.0"}, "version_mismatch"),
                              ({"version": "1.0.0"}, "version_mismatch"),
                              ({"permitted_owner": "S-1-5-21-OTHER"}, "owner_mismatch"),
                              ({"permitted_owner": "S-1-5-18"}, "owner_mismatch"),
                              ({"restart_required": 0}, "invalid_reply"),
                              ({"restart_required": True}, "restart_required")):
            with self.subTest(changes=changes), self.assertRaises(HotSwitchError) as raised:
                _status_reply(status(**changes), SID)
            self.assertEqual(raised.exception.code, code)

    def test_errors_keep_service_code_and_message(self):
        with self.assertRaises(HotSwitchError) as raised:
            _reply({"ok": False, "code": "unsupported_device", "message": "Unsupported input branch."})
        self.assertEqual((raised.exception.code, str(raised.exception)),
                         ("unsupported_device", "此手柄输入分支暂不支持自动切换。"))
        self.assertEqual(raised.exception.details, {"native_message": "Unsupported input branch."})
        for invalid in (None, [], {}, {"ok": 1}):
            with self.assertRaises(HotSwitchError):
                _reply(invalid)


class ErrorDetailsTests(unittest.TestCase):
    def test_usb_cycle_failure_keeps_exact_native_diagnostics(self):
        wire = {"ok": False, "code": "usb_cycle_failed", "version": "1.0.6",
                "message": "USB port cycle was rejected.", "method": "usb_hub_cycle_port",
                "root_id": r"USB\VID_37D7&PID_2502&MI_00\selected",
                "new_input_id": r"HID\VID_37D7&PID_2502&IG_01\selected",
                "physical_root_id": r"USB\VID_37D7&PID_2502\FLYDIGI_APEX_6",
                "hub_id": r"USB\ROOT_HUB30\selected", "port_index": 3,
                "cycle_win32_error": 5, "cycle_usb_status": 0xC0000001,
                "cycle_bytes": 8, "cycle_attempted": True, "restart_required": False,
                "input_status": 0x180200a, "input_problem": 0}
        with self.assertRaises(HotSwitchError) as raised:
            _reply(wire)
        details = raised.exception.details
        for key in ("physical_root_id", "hub_id", "new_input_id", "port_index", "cycle_win32_error",
                    "cycle_usb_status", "cycle_bytes", "cycle_attempted", "method", "input_status", "input_problem"):
            self.assertEqual(details[key], wire[key])
        self.assertEqual(str(raised.exception), "Windows 未能完成所选手柄的 USB 重连，请查看诊断记录。")
        roundtrip = HotSwitchError(raised.exception.code, str(raised.exception),
                                  details=json.loads(json.dumps(details)))
        self.assertEqual(roundtrip.details, details)

    def test_usb_diagnostics_reject_unknown_paths_types_and_ranges(self):
        details = {"physical_root_id": "x" * 513, "hub_id": "USB\\hub\0", "port_index": True,
                   "cycle_win32_error": -1, "cycle_usb_status": 2**32,
                   "cycle_bytes": "8", "cycle_attempted": 1, "hub_handle": 123}
        self.assertEqual(HotSwitchError("usb_cycle_failed", "failed", details=details).details, {})

    def native_failure(self):
        # Literal native wire fixture: no construction from client allowlists.
        return json.loads(r'''{"ok":false,"code":"recovery_pending",
            "message":"Windows has not confirmed that the original input is enabled.",
            "api_version":1,"version":"1.0.3","root_id":"USB\\VID_37D7&PID_2502&MI_00\\selected",
            "method":"cm_disable_enable","disable_result":34,"enable_result":0,
            "status":264,"problem":0,"status_before":264,"problem_before":0,
            "pre_needs_restart":true,"restart_required":true,"recovery_pending":true,
            "recovery_error":"recovery_failed","flags_before":128,"flags_after":384,"flags_added":256,
            "before":{"status":264,"problem":0,"started":true,"needs_restart":true}}''')

    def test_native_results_are_preserved_and_ui_message_is_short_chinese(self):
        wire = self.native_failure()
        with self.assertRaises(HotSwitchError) as raised:
            _reply(wire)
        details = raised.exception.details
        self.assertEqual(details, {"api_version": 1, "version": "1.0.3",
            "root_id": r"USB\VID_37D7&PID_2502&MI_00\selected", "method": "cm_disable_enable",
            "disable_result": 34, "enable_result": 0, "status": 264, "problem": 0,
            "status_before": 264, "problem_before": 0, "pre_needs_restart": True,
            "restart_required": True, "recovery_pending": True, "recovery_error": "recovery_failed",
            "flags_before": 128, "flags_after": 384, "flags_added": 256,
            "before": {"status": 264, "problem": 0, "started": True, "needs_restart": True},
            "native_message": "Windows has not confirmed that the original input is enabled."})
        self.assertEqual(str(raised.exception), "Windows 仍要求重启电脑，输入切换尚未完成。")
        wire["before"]["status"] = 0
        self.assertEqual(details["before"]["status"], 264)

    def test_details_survive_native_exception_worker_json_and_parent_exception(self):
        wire = self.native_failure()
        def native_reply(_request):
            return _reply(wire)
        token = base64.b64encode(json.dumps({"version": 1, "operation": "restart", "device_id": DEVICE}).encode()).decode()
        with patch("dsbridge.platform.windows.hot_switch_service._Native") as native, \
                patch("dsbridge.platform.windows.hot_switch_service._emit") as emit:
            native.return_value.request.side_effect = native_reply
            self.assertEqual(worker_main(token), 0)
        worker_json = json.dumps(emit.call_args.args[0]).encode()
        child = MagicMock(returncode=0)
        child.poll.return_value = 0
        child.communicate.return_value = (worker_json, None)
        client = HotSwitchClient(RuntimePaths(Path.cwd(), Path.cwd()))
        with patch("dsbridge.platform.windows.hot_switch_service.subprocess.Popen", return_value=child), \
                self.assertRaises(HotSwitchError) as raised:
            client._request(_wire_request("restart", DEVICE), lambda: None)
        self.assertEqual(raised.exception.code, "recovery_pending")
        self.assertEqual(raised.exception.details, emit.call_args.args[0]["details"])
        self.assertEqual(raised.exception.details["disable_result"], 34)
        self.assertEqual(raised.exception.details["enable_result"], 0)
        self.assertTrue(raised.exception.details["restart_required"])
        self.assertEqual(raised.exception.details["native_message"], wire["message"])
        self.assertEqual(str(raised.exception), "Windows 仍要求重启电脑，输入切换尚未完成。")
        child.kill.assert_not_called()

    def test_restart_wording_requires_a_real_boolean_flag(self):
        for flag in (False, 1, "true"):
            wire = self.native_failure()
            wire["restart_required"] = flag
            with self.subTest(flag=flag), self.assertRaises(HotSwitchError) as raised:
                _reply(wire)
            self.assertEqual(str(raised.exception), "手柄输入仍待恢复，请查看诊断记录后重试。")

    def test_unknown_unbounded_and_wrongly_typed_fields_are_dropped(self):
        details = {"disable_result": True, "enable_result": "0", "status": -1, "problem": 0.0,
                   "status_before": 4294967296, "problem_before": 0, "flags_before": 4294967295,
                   "restart_required": 1, "pre_needs_restart": "true", "recovery_pending": False,
                   "root_id": "x" * 513, "method": "cm disable", "recovery_error": "bad\nvalue",
                   "native_message": "x" * 513, "version": "arbitrary", "api_version": True,
                   "before": {"status": True, "problem": 0, "started": 1,
                              "needs_restart": False, "nested": {"secret": "do not retain"}},
                   "raw_payload": [1, 2], "permitted_owner": SID, "anything": "do not retain"}
        self.assertEqual(HotSwitchError("failed", "failure", details=details).details,
                         {"problem_before": 0, "flags_before": 4294967295, "recovery_pending": False,
                          "before": {"problem": 0, "needs_restart": False}})

    def test_valid_limits_and_control_character_rejection(self):
        self.assertEqual(HotSwitchError("failed", "failure", details={"root_id": "x" * 512}).details,
                         {"root_id": "x" * 512})
        for value in (None, [], {"root_id": "USB\\bad\0id"}, {"root_id": "USB\\bad\x7fid"},
                      {"root_id": "USB\\bad\x85id"},
                      {"before": {"status": {"nested": 0}}}):
            with self.subTest(value=value):
                self.assertEqual(HotSwitchError("failed", "failure", details=value).details, {})

    def test_success_status_restart_requirement_retains_diagnostics(self):
        with self.assertRaises(HotSwitchError) as raised:
            _status_reply(status(restart_required=True, status=264, problem=0), SID)
        self.assertEqual(raised.exception.code, "restart_required")
        self.assertEqual(raised.exception.details["status"], 264)
        self.assertIs(raised.exception.details["restart_required"], True)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = HotSwitchClient(RuntimePaths(Path.cwd(), Path.cwd()), timeout=0.5)

    def test_restart_checks_status_identity_before_sending_restart(self):
        with patch("dsbridge.platform.windows.hot_switch_service.current_user_sid", return_value=SID), \
                patch.object(self.client, "_request", side_effect=[status(), {"ok": True}]) as request:
            result = self.client.restart(DEVICE)
        self.assertEqual(result, {"ok": True})
        self.assertEqual([call.args[0]["operation"] for call in request.call_args_list], ["status", "restart"])

    def test_failed_identity_prevents_restart_request(self):
        with patch("dsbridge.platform.windows.hot_switch_service.current_user_sid", return_value=SID), \
                patch.object(self.client, "_request", return_value=status(permitted_owner="S-1-5-18")) as request:
            with self.assertRaises(HotSwitchError) as raised:
                self.client.restart(DEVICE)
        self.assertEqual(raised.exception.code, "owner_mismatch")
        self.assertEqual(request.call_count, 1)

    def test_invalid_device_does_not_even_query_status(self):
        with patch.object(self.client, "status") as check:
            with self.assertRaises(HotSwitchError):
                self.client.restart("HID\\*")
        check.assert_not_called()

    def test_timeout_stops_only_owned_worker_and_marks_uncertain_restart(self):
        child = MagicMock()
        child.communicate.side_effect = [(b"", None)]
        child.poll.return_value = None
        with patch("dsbridge.platform.windows.hot_switch_service.subprocess.Popen", return_value=child), \
                patch("dsbridge.platform.windows.hot_switch_service.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaises(HotSwitchError) as raised:
                self.client._request(_wire_request("restart", DEVICE), lambda: None)
        child.kill.assert_called_once_with()
        self.assertEqual(raised.exception.code, "service_timeout")
        self.assertTrue(raised.exception.may_have_restarted)

    def test_cancel_exception_is_preserved_and_owned_worker_stops(self):
        class Cancelled(Exception):
            pass
        child = MagicMock()
        child.poll.return_value = None
        child.communicate.side_effect = [subprocess.TimeoutExpired("worker", 0.1), (b"", None)]
        check = MagicMock(side_effect=[None, None, Cancelled("stop requested")])
        with patch("dsbridge.platform.windows.hot_switch_service.subprocess.Popen", return_value=child):
            with self.assertRaisesRegex(Cancelled, "stop requested"):
                self.client._request(_wire_request("status"), check)
        child.kill.assert_called_once_with()
        self.assertLessEqual(child.communicate.call_args_list[0].kwargs["timeout"], 0.1)

    def test_success_cleans_no_unrelated_process(self):
        child = MagicMock(returncode=0)
        child.poll.return_value = 0
        child.communicate.return_value = (json.dumps(status()).encode(), None)
        with patch("dsbridge.platform.windows.hot_switch_service.subprocess.Popen", return_value=child) as launch:
            self.assertEqual(self.client._request(_wire_request("status"), lambda: None), status())
        child.kill.assert_not_called()
        request = _worker_request(launch.call_args.args[0][-1])
        self.assertEqual(request, {"version": 1, "operation": "status"})

    def test_source_client_can_inspect_a_separate_packaged_build(self):
        # The audit runner imports source but points its assets at a release.
        # Its Python worker must still resolve dsbridge from the source root.
        source_root = Path(__file__).resolve().parents[1]
        packaged_assets = source_root / "发布" / "example-build"
        client = HotSwitchClient(RuntimePaths(packaged_assets, source_root / "测试记录"))
        child = MagicMock(returncode=0)
        child.poll.return_value = 0
        child.communicate.return_value = (json.dumps(status()).encode(), None)
        with patch("dsbridge.platform.windows.hot_switch_service.sys.frozen", False, create=True), \
                patch("dsbridge.platform.windows.hot_switch_service.subprocess.Popen", return_value=child) as launch:
            client._request(_wire_request("status"), lambda: None)
        self.assertEqual(launch.call_args.kwargs["cwd"], source_root)
        self.assertNotEqual(launch.call_args.kwargs["cwd"], packaged_assets)

    def test_malformed_or_failed_child_is_not_success(self):
        for output, returncode, code in ((b"not json", 0, "invalid_reply"), (b"{}", 3, "worker_failed")):
            child = MagicMock(returncode=returncode)
            child.poll.return_value = returncode
            child.communicate.return_value = (output, None)
            with patch("dsbridge.platform.windows.hot_switch_service.subprocess.Popen", return_value=child):
                with self.assertRaises(HotSwitchError) as raised:
                    self.client._request(_wire_request("status"), lambda: None)
            self.assertEqual(raised.exception.code, code)


class NativePipeTests(unittest.TestCase):
    def native(self):
        native = object.__new__(_Native)
        native.k = MagicMock()
        native.k.CreateFileW.return_value = 55
        native.service_pid = MagicMock(return_value=42)
        return native

    def test_server_pid_must_match_registered_service_before_any_write(self):
        native = self.native()
        def mismatched(_handle, pid):
            C.cast(pid, C.POINTER(C.c_uint32)).contents.value = 99
            return 1
        native.k.GetNamedPipeServerProcessId.side_effect = mismatched
        with self.assertRaises(HotSwitchError) as raised:
            native.request(_wire_request("restart", DEVICE))
        self.assertEqual(raised.exception.code, "server_identity_mismatch")
        native.k.WriteFile.assert_not_called()
        native.k.CloseHandle.assert_called_once_with(55)

    def test_pipe_uses_exact_acl_mask_and_identification_only(self):
        native = self.native()
        native.k.GetNamedPipeServerProcessId.return_value = 0
        with self.assertRaises(HotSwitchError):
            native.request(_wire_request("status"))
        native.k.CreateFileW.assert_called_once_with(PIPE_NAME, 0x0012019B, 0, None, 3, 0x00110000, None)

    def test_valid_response_is_framed_and_handle_is_closed(self):
        native = self.native()
        def server(_handle, pointer):
            C.cast(pointer, C.POINTER(C.c_uint32)).contents.value = 42
            return 1
        def write(_handle, _data, size, count, _overlapped):
            C.cast(count, C.POINTER(C.c_uint32)).contents.value = size
            return 1
        def read(_handle, buffer, _size, count, _overlapped):
            data = json.dumps(status()).encode() + b"\n"
            C.memmove(buffer, data, len(data))
            C.cast(count, C.POINTER(C.c_uint32)).contents.value = len(data)
            return 1
        native.k.GetNamedPipeServerProcessId.side_effect = server
        native.k.WriteFile.side_effect = write
        native.k.ReadFile.side_effect = read
        self.assertEqual(native.request(_wire_request("status")), status())
        native.k.CloseHandle.assert_called_once_with(55)
        self.assertEqual(native.service_pid.call_count, 2)


class ArtifactTests(unittest.TestCase):
    def test_only_exact_bundled_or_development_location_is_used(self):
        # Mock existence rather than creating files requiring cleanup.
        paths = RuntimePaths(Path.cwd(), Path.cwd())
        expected = paths.assets / "native" / "hotswitch" / "bin" / "DSBridgeHotSwitch.exe"
        with patch.object(Path, "is_file", lambda p: p == expected):
            self.assertEqual(helper_artifact(paths), expected)
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(HotSwitchError) as raised:
                helper_artifact(paths)
        self.assertEqual(raised.exception.code, "helper_missing")


if __name__ == "__main__":
    unittest.main()
