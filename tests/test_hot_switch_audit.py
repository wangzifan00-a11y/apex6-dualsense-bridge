"""Evidence requirements are stricter than a newly started visibility check."""
import ctypes as C
from dataclasses import replace
import errno
import itertools
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from dsbridge.diagnostics.hot_switch import (
    Evidence, _Reader, _ZeroInput, _physical_connected, _present_dualsense_count,
    _current_connection, _verify_hidden_input, _running_audio_snapshot, _running_isolation_snapshot, _write_observer_final,
    audit_passed, observer_main, read, save_json,
)
from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows.input_transition_targets import InputTargets


class JsonIpcTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dsbridge-json-ipc-", dir=Path(__file__).parent))
        self.path = self.root / "control.json"
        # Remove only these explicitly named test files, then the empty folder.
        self.addCleanup(self.root.rmdir)
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.addCleanup(self.path.with_suffix(".tmp").unlink, missing_ok=True)

    @staticmethod
    def denied(winerror=None):
        error = PermissionError(errno.EACCES, "temporary sharing conflict")
        if winerror is not None:
            error.winerror = winerror
        return error

    def test_control_and_report_reads_retry_windows_and_crt_sharing_errors(self):
        for winerror in (5, 32, 33, None):
            with self.subTest(winerror=winerror), \
                 patch.object(Path, "read_text", side_effect=[self.denied(winerror), '{"phase": "restored"}']) as reader, \
                 patch("dsbridge.diagnostics.hot_switch.time.sleep") as sleep:
                self.assertEqual(read(self.path), {"phase": "restored"})
                self.assertEqual(reader.call_count, 2)
                sleep.assert_called_once()

    def test_permanent_permission_failure_is_bounded_and_propagates_original_error(self):
        error = self.denied(5)
        with patch.object(Path, "read_text", side_effect=error) as reader, \
             patch("dsbridge.diagnostics.hot_switch._JSON_IO_ATTEMPTS", 3), \
             patch("dsbridge.diagnostics.hot_switch.time.sleep") as sleep:
            with self.assertRaises(PermissionError) as raised:
                read(self.path)
        self.assertIs(raised.exception, error)
        self.assertEqual(reader.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_missing_file_unrelated_windows_error_and_invalid_json_are_not_retried(self):
        unrelated = self.denied(123)
        for error in (FileNotFoundError(errno.ENOENT, "missing"), unrelated):
            with self.subTest(error=error), patch.object(Path, "read_text", side_effect=error) as reader, \
                 patch("dsbridge.diagnostics.hot_switch.time.sleep") as sleep:
                with self.assertRaises(type(error)):
                    read(self.path)
                reader.assert_called_once()
                sleep.assert_not_called()
        with patch.object(Path, "read_text", side_effect=['{"partial":', '{"later": true}']) as reader, \
             patch("dsbridge.diagnostics.hot_switch.time.sleep") as sleep:
            with self.assertRaises(json.JSONDecodeError):
                read(self.path)
            reader.assert_called_once()
            sleep.assert_not_called()

    def test_replace_retries_publish_only_the_fully_written_payload(self):
        previous = {"phase": "bridge"}
        expected = {"phase": "restored", "message": "恢复完成", "reports": [1, 2, 3]}
        self.path.write_text(json.dumps(previous), encoding="utf-8")
        actual_replace = Path.replace
        attempts = []

        def replace_after_reader_releases(source, target):
            attempts.append(source)
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), expected)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), previous)
            if len(attempts) < 3:
                raise self.denied(32)
            return actual_replace(source, target)

        with patch.object(Path, "replace", autospec=True, side_effect=replace_after_reader_releases), \
             patch("dsbridge.diagnostics.hot_switch.time.sleep") as sleep:
            save_json(self.path, expected)
        self.assertEqual(read(self.path), expected)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_failed_publication_preserves_old_message_and_propagates(self):
        self.path.write_text('{"phase": "bridge"}', encoding="utf-8")
        error = self.denied(5)
        with patch.object(Path, "replace", side_effect=error) as replace_file, \
             patch("dsbridge.diagnostics.hot_switch._JSON_IO_ATTEMPTS", 3), \
             patch("dsbridge.diagnostics.hot_switch.time.sleep"):
            with self.assertRaises(PermissionError) as raised:
                save_json(self.path, {"phase": "restored"})
        self.assertIs(raised.exception, error)
        self.assertEqual(replace_file.call_count, 3)
        self.assertEqual(read(self.path), {"phase": "bridge"})
        self.assertEqual(read(self.path.with_suffix(".tmp")), {"phase": "restored"})


class EvidenceTests(unittest.TestCase):
    def observe(self, audit, phase, **changes):
        args = dict(physical=True, sony_count=0, old_read_started=True, old_reports=1,
                    old_error=None, fresh_open=False, fresh_reports=0)
        args.update(changes)
        audit.observe(phase, **args)

    def test_full_old_connection_switch_and_separate_fresh_recovery(self):
        audit = Evidence()
        self.observe(audit, "baseline")
        for at in (10, 10.4, 10.8):
            self.observe(audit, "bridge", physical=False, sony_count=1, old_error=1167, at=at)
        self.assertTrue(audit.switched())
        self.assertFalse(audit.restored())
        for at in (20, 20.4, 20.8):
            self.observe(audit, "restored", fresh_open=True, fresh_reports=1, at=at)
        self.assertTrue(audit.restored())
        self.assertEqual(audit.value["old_hid_open_count"], 1)

    def test_new_device_enumeration_without_dead_old_handle_is_not_a_pass(self):
        audit = Evidence()
        self.observe(audit, "baseline")
        self.observe(audit, "bridge", physical=False, sony_count=1)
        self.observe(audit, "restored", fresh_open=True, fresh_reports=4)
        self.assertFalse(audit.switched())
        self.assertFalse(audit.restored())

    def test_cancelled_read_alone_does_not_prove_handle_invalidation(self):
        for error in (31, 1117, 995, 996):
            audit = Evidence()
            self.observe(audit, "baseline")
            self.observe(audit, "bridge", physical=False, sony_count=1, old_error=error)
            with self.subTest(error=error):
                self.assertFalse(audit.value["old_hid_invalidated"])

    def test_brief_disconnect_then_cached_xinput_is_not_stable_hidden(self):
        audit = Evidence()
        self.observe(audit, "baseline")
        for at in (1, 1.2, 1.4):
            self.observe(audit, "bridge", physical=False, sony_count=1, old_error=433, at=at)
        self.assertFalse(audit.switched())
        self.observe(audit, "bridge", physical=True, sony_count=1, old_error=433, at=1.5)
        for at in (1.6, 2, 2.2):
            self.observe(audit, "bridge", physical=False, sony_count=1, old_error=433, at=at)
        self.assertFalse(audit.switched())
        self.observe(audit, "bridge", physical=False, sony_count=1, old_error=433, at=2.4)
        self.assertTrue(audit.switched())

    def test_restoration_requires_a_fresh_hid_report_and_current_xinput(self):
        audit = Evidence()
        self.observe(audit, "baseline")
        for at in (1, 1.4, 1.8):
            self.observe(audit, "bridge", physical=False, sony_count=1, old_error=1167, at=at)
        for at in (2, 2.4, 2.8):
            self.observe(audit, "restored", fresh_open=True, fresh_reports=0, at=at)
        self.assertFalse(audit.restored())
        for at in (3, 3.4, 3.8):
            self.observe(audit, "restored", fresh_open=True, fresh_reports=1, physical=False, at=at)
        self.assertFalse(audit.restored())
        for at in (4, 4.4, 4.8):
            self.observe(audit, "restored", fresh_open=True, fresh_reports=2, at=at)
        self.assertTrue(audit.restored())

    def test_missing_baseline_or_preexisting_sony_fails(self):
        with self.assertRaisesRegex(RuntimeError, "缺少"):
            self.observe(Evidence(), "bridge", physical=False, sony_count=1, old_error=1167)
        with self.assertRaisesRegex(RuntimeError, "已有虚拟 DS"):
            self.observe(Evidence(), "baseline", sony_count=1)

    def test_duplicate_sony_during_transition_fails_instead_of_becoming_one_later(self):
        audit = Evidence()
        self.observe(audit, "baseline")
        with self.assertRaisesRegex(RuntimeError, "多个 DS"):
            self.observe(audit, "bridge", physical=False, sony_count=2, old_error=1167)

    def test_switch_requires_an_old_physical_consumer_not_just_open_count(self):
        audit = Evidence()
        self.observe(audit, "baseline", old_read_started=False)
        self.assertFalse(audit.value["baseline"])
        self.observe(audit, "baseline", physical=False)
        self.assertFalse(audit.value["baseline"])


class PhysicalConnectionTests(unittest.TestCase):
    connection = SimpleNamespace(vendor_id=0x045E, product_id=0x028E)

    def test_live_get_state_with_failed_identity_cannot_prove_disconnection(self):
        # Exercise the actual identity implementation: successful GetState and
        # unavailable extended capabilities produce a live, unidentified slot.
        from dsbridge.platform.windows.xinput import XInput
        xinput = XInput.__new__(XInput)
        xinput._ensure_com = MagicMock()
        xinput.get_state = MagicMock(side_effect=lambda slot: {"index": slot} if slot == 0 else None)
        xinput._dll = MagicMock()
        xinput._dll.__getitem__.return_value.return_value = 1167
        xinput._dll.XInputGetCapabilities.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "仍在线但身份无法确认"):
            _physical_connected(xinput, self.connection)
        xinput.get_state.assert_called_once_with(0)

    def test_explicitly_disconnected_slots_and_rebound_slot_are_distinct(self):
        xinput = SimpleNamespace(identity=MagicMock(return_value=None))
        self.assertFalse(_physical_connected(xinput, self.connection))
        self.assertEqual(xinput.identity.call_args_list, [call(i) for i in range(4)])
        xinput.identity.side_effect = [None, None, {"vendor_id": 0x045E, "product_id": 0x028E}, None]
        self.assertTrue(_physical_connected(xinput, self.connection))

    def test_unknown_extra_slot_is_not_ignored_after_a_known_match(self):
        xinput = SimpleNamespace(identity=MagicMock(side_effect=[
            {"vendor_id": 0x045E, "product_id": 0x028E},
            {"vendor_id": None, "product_id": None}]))
        with self.assertRaisesRegex(RuntimeError, "槽位 1"):
            _physical_connected(xinput, self.connection)


class ReportTests(unittest.TestCase):
    def valid(self):
        audit = Evidence()
        audit.value.update(baseline=True, old_hid_invalidated=True, old_hid_error=1167,
                           physical_xinput_absent=True, virtual_sony_arrived=True,
                           physical_xinput_restored=True, fresh_hid_opened_after_stop=True, virtual_sony_removed=True,
                           hidden_stable=True, restored_stable=True, fresh_hid_reports=3)
        root = r"USB\VID_37D7&PID_2502&MI_00\RECEIVER"
        old = [r"HID\VID_37D7&PID_2502&IG_00\OLD", r"USB\VID_37D7&PID_2502&IG_00\OLD", root]
        current = [r"HID\VID_37D7&PID_2502&IG_01\CURRENT", r"USB\VID_37D7&PID_2502&IG_01\CURRENT", root]
        before_last = old + current[:2]
        physical, hub = r"USB\VID_37D7&PID_2502\PHYSICAL", r"USB\ROOT_HUB30\HUB"
        isolation = dict(phase="ready", hidden=True, verified_current_input_id=current[0],
            current_targets=dict(device_id=current[0], restart_id=root, hidden_ids=list(current), transport="receiver"),
            input_anchor=dict(root_id=root, physical_root_id=physical, hub_id=hub),
            restart_result=dict(ok=True, restart_required=False, root_id=root, physical_root_id=physical,
                                hub_id=hub, method="usb_hub_cycle_port", cycle_attempted=True, new_input_id=current[0]),
            isolation=dict(converged=True, rounds=2, hidden_before_last_refresh=list(before_last),
                           final_hidden_ids=list(current), final_input_id=current[0], evidence=[
                dict(round=1, active=True, inverse=False, pre_hidden=list(old), current=list(current),
                     input_id=current[0], verified=False, native_success=True),
                dict(round=2, active=True, inverse=False, pre_hidden=list(before_last), current=list(current),
                     input_id=current[0], verified=True, native_success=True)]))
        return dict(engine_stopped=True, observer_exited=True, zero_gain=True, game_validation=False,
                    transport="receiver", baseline={"restart_id": root}, during_isolation=isolation,
                    observer=dict(passed=True, no_output=True, cleanup_complete=True,
                                  sony_enumeration_complete=True, fresh_physical_anchor_verified=True,
                                  hidden_input={"blocked": True, "win32_error": 5,
                                                "physical_anchor_verified": True, "no_output": True, "device_id": current[0]},
                                  evidence=dict(audit.value)),
                    before={"audio_defaults": {"0:0": {"id": "speakers"}}, "hidhide": 1},
                    after={"audio_defaults": {"0:0": {"id": "speakers"}}, "hidhide": 1},
                    during_audio={"defaults": {"0:0": {"id": "speakers"}},
                                  "protection_before": {"active": True, "error": None},
                                  "protection_after": {"active": True, "error": None}},
                    motor_before={"mapping": "00"}, motor_after={"mapping": "00"},
                    nonzero_input_attempts=0, motor_nonzero_writes=0,
                    session_end={"phase": "stopped", "gain": 0, "output_nonzero_writes": 0, "output_peak": [0, 0]})

    def test_complete_evidence_and_exact_restore_are_required(self):
        self.assertTrue(audit_passed(self.valid()))
        for field, wrong in (("engine_stopped", False), ("observer_exited", False),
                             ("after", {"audio": 2}), ("motor_after", {"mapping": "01"}),
                             ("nonzero_input_attempts", 1), ("motor_nonzero_writes", 1),
                             ("cleanup_errors", ["pending read"]), ("error", "failed")):
            value = self.valid()
            value[field] = wrong
            with self.subTest(field=field):
                self.assertFalse(audit_passed(value))

    def test_every_explicit_observation_must_be_present(self):
        for field in ("baseline", "old_hid_invalidated", "physical_xinput_absent", "virtual_sony_arrived",
                      "physical_xinput_restored", "fresh_hid_opened_after_stop", "virtual_sony_removed", "hidden_stable", "restored_stable"):
            value = self.valid()
            del value["observer"]["evidence"][field]
            with self.subTest(field=field):
                self.assertFalse(audit_passed(value))
        value = self.valid()
        value["observer"]["evidence"]["old_hid_open_count"] = 2
        self.assertFalse(audit_passed(value))

    def test_missing_output_or_restoration_records_cannot_silently_pass(self):
        for field in ("before", "after", "during_audio", "during_isolation", "motor_before", "motor_after", "session_end", "motor_nonzero_writes", "zero_gain"):
            value = self.valid()
            del value[field]
            with self.subTest(field=field):
                self.assertFalse(audit_passed(value))

    def test_hiding_a_new_generation_after_refresh_is_not_converged_proof(self):
        value = self.valid()
        proof = value["during_isolation"]["isolation"]
        # Fresh opens are denied, but this ID was not hidden before the last
        # cycle: a consumer could have opened it during the migration window.
        proof["hidden_before_last_refresh"] = proof["evidence"][0]["pre_hidden"]
        self.assertTrue(value["observer"]["hidden_input"]["blocked"])
        self.assertFalse(audit_passed(value))
        value = self.valid()
        value["during_isolation"]["isolation"]["evidence"][-1]["pre_hidden"].pop()
        self.assertFalse(audit_passed(value))

    def test_isolation_rounds_and_native_success_must_be_explicit(self):
        for field, wrong in (("converged", False), ("rounds", True), ("rounds", 3), ("evidence", [])):
            value = self.valid()
            value["during_isolation"]["isolation"][field] = wrong
            with self.subTest(field=field, wrong=wrong):
                self.assertFalse(audit_passed(value))
        for field, wrong in (("active", False), ("inverse", True), ("native_success", False),
                             ("verified", False), ("round", 1)):
            value = self.valid()
            value["during_isolation"]["isolation"]["evidence"][-1][field] = wrong
            with self.subTest(field=field):
                self.assertFalse(audit_passed(value))

    def test_isolation_matches_same_ready_generation_and_physical_root(self):
        for where, field in (("current_targets", "device_id"), ("current_targets", "restart_id"),
                             ("restart_result", "new_input_id"), ("restart_result", "physical_root_id"),
                             ("restart_result", "hub_id"), ("input_anchor", "root_id")):
            value = self.valid()
            value["during_isolation"][where][field] = "OTHER_DEVICE"
            with self.subTest(where=where, field=field):
                self.assertFalse(audit_passed(value))
        value = self.valid()
        value["observer"]["hidden_input"]["device_id"] = "ANOTHER_GENERATION"
        self.assertFalse(audit_passed(value))
        value = self.valid()
        value["during_isolation"]["phase"] = "finished"
        self.assertFalse(audit_passed(value))

    def test_single_round_is_valid_when_same_ids_were_already_hidden(self):
        value = self.valid()
        proof = value["during_isolation"]["isolation"]
        proof["rounds"] = 1
        proof["evidence"] = [proof["evidence"][-1]]
        proof["evidence"][0]["round"] = 1
        self.assertTrue(audit_passed(value))

    def test_isolation_snapshot_preserves_ready_evidence_before_stop_mutates_state(self):
        state = self.valid()["during_isolation"]
        lease = SimpleNamespace(status=lambda: state,
                                current_connection=SimpleNamespace(device_id=state["verified_current_input_id"]))
        transition = SimpleNamespace(status=lambda: {"phase": "ready"}, error=None, _lease=lease)
        snapshot = _running_isolation_snapshot(transition)
        state["phase"] = "finished"
        state["current_targets"]["device_id"] = "NEXT_GENERATION_AFTER_STOP"
        self.assertEqual(snapshot["phase"], "ready")
        self.assertEqual(snapshot["current_targets"]["device_id"], snapshot["verified_current_input_id"])
        with self.assertRaisesRegex(RuntimeError, "尚未就绪"):
            _running_isolation_snapshot(transition)

    def test_audio_restored_at_end_does_not_excuse_changed_running_defaults(self):
        value = self.valid()
        value["during_audio"]["defaults"]["0:0"]["id"] = "sony-speakers"
        self.assertEqual(value["before"], value["after"])
        self.assertFalse(audit_passed(value))

    def test_running_audio_guard_must_be_active_before_and_after_snapshot(self):
        for key in ("protection_before", "protection_after"):
            for wrong in (None, {}, {"active": False}, {"active": True, "error": "failed"}):
                value = self.valid()
                value["during_audio"][key] = wrong
                with self.subTest(key=key, wrong=wrong):
                    self.assertFalse(audit_passed(value))

    def test_old_readable_only_sony_enumeration_is_not_complete_proof(self):
        value = self.valid()
        del value["observer"]["sony_enumeration_complete"]
        self.assertFalse(audit_passed(value))

    def test_unbound_fresh_hid_or_unproven_new_open_denial_cannot_pass(self):
        value = self.valid()
        value["observer"]["fresh_physical_anchor_verified"] = False
        self.assertFalse(audit_passed(value))
        for hidden in ({}, {"blocked": True, "win32_error": 1167},
                       {"blocked": True, "win32_error": 5, "physical_anchor_verified": False}):
            value = self.valid()
            value["observer"]["hidden_input"] = hidden
            self.assertFalse(audit_passed(value))

    def test_cancelled_old_handle_or_failed_engine_is_not_success(self):
        value = self.valid()
        value["observer"]["evidence"]["old_hid_error"] = 995
        self.assertFalse(audit_passed(value))
        for field, wrong in (("phase", "failed"), ("error", "stopping failed"), ("gain", .7), ("output_peak", [])):
            value = self.valid()
            value["session_end"][field] = wrong
            with self.subTest(field=field):
                self.assertFalse(audit_passed(value))

    def test_missing_fresh_completed_report_cannot_pass(self):
        for count in (None, 0, -1, True):
            value = self.valid()
            value["observer"]["evidence"]["fresh_hid_reports"] = count
            with self.subTest(count=count):
                self.assertFalse(audit_passed(value))

    def test_read_request_must_be_pending_or_have_completed_reports(self):
        reader = _Reader.__new__(_Reader)
        reader.started, reader.pending, reader.reports, reader.error = 1, False, 0, None
        self.assertFalse(reader.read_active)
        reader.pending = True
        self.assertTrue(reader.read_active)
        reader.pending, reader.reports = False, 1
        self.assertTrue(reader.read_active)
        reader.error = 1167
        self.assertFalse(reader.read_active)


class ZeroOutputTests(unittest.TestCase):
    def test_only_zero_output_is_forwarded(self):
        class Source:
            def __init__(self):
                self.writes = []
            def set_rumble(self, *values):
                self.writes.append(values)
        source = Source()
        guarded = _ZeroInput(source)
        guarded.set_rumble(0, 0, 0)
        with self.assertRaises(RuntimeError):
            guarded.set_rumble(0, 0.01, 0)
        self.assertEqual(source.writes, [(0, 0, 0)])
        self.assertEqual(guarded.nonzero_attempts, 1)


class PresentSonyTests(unittest.TestCase):
    def api(self, count=1):
        api = MagicMock()
        api.setup.SetupDiGetClassDevsW.return_value = 10
        api.setup.SetupDiEnumDeviceInterfaces.side_effect = lambda _info, _device, _guid, index, _item: index < count
        path = C.create_unicode_buffer(r"\\?\hid#vid_054c&pid_0ce6#test")

        def detail(_info, _interface, buffer, _capacity, required, _devinfo):
            required._obj.value = 4 + C.sizeof(path)
            if buffer is None:
                return False
            C.memmove(C.addressof(buffer) + 4, path, C.sizeof(path))
            return True

        api.setup.SetupDiGetDeviceInterfaceDetailW.side_effect = detail
        api.open.return_value = 55
        api.inspect.return_value = (SimpleNamespace(VendorID=0x054C, ProductID=0x0CE6),
                                    SimpleNamespace(UsagePage=1, Usage=5, InputReportByteLength=64,
                                                    OutputReportByteLength=48))
        api.usbip_ancestry.return_value = [{"instance_id": "ROOT\\USBIP", "service": "usbip2_ude"}]
        return api

    def count(self, api):
        with patch("dsbridge.diagnostics.sony_writer._Api", return_value=api), \
                patch("dsbridge.diagnostics.hot_switch.C.get_last_error", return_value=259):
            return _present_dualsense_count()

    def test_complete_present_enumeration_counts_zero_one_and_two(self):
        for count in (0, 1, 2):
            api = self.api(count)
            with self.subTest(count=count):
                self.assertEqual(self.count(api), count)
                api.setup.SetupDiDestroyDeviceInfoList.assert_called_once_with(10)
                self.assertEqual(api.open.call_count, count)
                api.kernel.WriteFile.assert_not_called()

    def test_sony_open_or_capability_failure_is_unknown_not_absence(self):
        from dsbridge.diagnostics.sony_writer import SonyWriterError
        for name in ("open", "inspect"):
            api = self.api()
            getattr(api, name).side_effect = SonyWriterError("unreadable candidate")
            with self.subTest(name=name), self.assertRaisesRegex(SonyWriterError, "unreadable"):
                self.count(api)
            api.setup.SetupDiDestroyDeviceInfoList.assert_called_once_with(10)
            if name == "inspect":
                api.kernel.CloseHandle.assert_called_once_with(55)

    def test_detail_failure_cannot_hide_an_unclassified_interface(self):
        api = self.api()
        original = api.setup.SetupDiGetDeviceInterfaceDetailW.side_effect
        api.setup.SetupDiGetDeviceInterfaceDetailW.side_effect = lambda *args: original(*args) if args[2] is None else False
        with self.assertRaisesRegex(RuntimeError, "路径查询失败"):
            self.count(api)
        api.setup.SetupDiDestroyDeviceInfoList.assert_called_once_with(10)
        api.open.assert_not_called()

    def test_unconfirmed_sony_ancestry_is_not_silently_excluded(self):
        api = self.api()
        api.usbip_ancestry.return_value = None
        with self.assertRaisesRegex(RuntimeError, "无法确认属于"):
            self.count(api)


class RunningAudioTests(unittest.TestCase):
    def test_protection_is_read_on_both_sides_of_independent_windows_snapshot(self):
        calls = []
        statuses = iter(({"audio_protection": {"active": True, "error": None}},
                         {"audio_protection": {"active": False, "error": "stopped"}}))
        def status():
            calls.append("guard")
            return next(statuses)
        def snapshot():
            calls.append("windows")
            return {"0:0": {"id": "speakers"}}
        with patch("dsbridge.diagnostics.hot_switch._audio_defaults", side_effect=snapshot):
            result = _running_audio_snapshot(SimpleNamespace(status=status))
        self.assertEqual(calls, ["guard", "windows", "guard"])
        self.assertTrue(result["protection_before"]["active"])
        self.assertFalse(result["protection_after"]["active"])


class MigratedObserverTests(unittest.TestCase):
    def setUp(self):
        self.original = ControllerConnection("receiver", device_id=r"HID\VID_37D7&PID_2502&IG_00\old",
                                             vendor_id=0x37D7, product_id=0x2502)
        self.current = replace(self.original, device_id=r"HID\VID_37D7&PID_2502&IG_02\new")
        self.targets = InputTargets(self.original.device_id, r"USB\VID_37D7&PID_2502&MI_00\root",
                                    (self.original.device_id,), (self.original.device_id,), "receiver")
        self.current_targets = replace(self.targets, device_id=self.current.device_id)
        self.anchor = {"root": "original-physical-fingerprint"}

    def test_new_leaf_requires_original_root_not_just_same_vid_pid(self):
        with patch("dsbridge.platform.windows.input_transition_targets.resolve_current_input",
                   return_value=self.current_targets) as resolve:
            self.assertEqual(_current_connection(self.original, self.targets, self.anchor), self.current)
            resolve.assert_called_once_with(self.targets, self.anchor)
            resolve.return_value = replace(self.current_targets, restart_id=self.targets.restart_id + "OTHER")
            with self.assertRaisesRegex(RuntimeError, "原物理手柄"):
                _current_connection(self.original, self.targets, self.anchor)

    def test_new_hid_open_accepts_only_access_denied_with_stable_identity(self):
        from dsbridge.diagnostics.sony_writer import INVALID_HANDLE_VALUE
        api = MagicMock()
        api.kernel.CreateFileW.return_value = INVALID_HANDLE_VALUE
        with patch("dsbridge.diagnostics.sony_writer._Api", return_value=api), \
                patch("dsbridge.diagnostics.hot_switch._current_connection", return_value=self.current) as resolve, \
                patch("dsbridge.diagnostics.hot_switch._physical_path", return_value="current-path"), \
                patch("dsbridge.diagnostics.hot_switch.C.get_last_error", return_value=5) as error:
            result = _verify_hidden_input(self.original, self.targets, self.anchor)
            self.assertTrue(result["blocked"])
            self.assertEqual(result["device_id"], self.current.device_id)
            for code in (2, 32, 433, 1167):
                error.return_value = code
                with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, "不是明确拒绝访问"):
                    _verify_hidden_input(self.original, self.targets, self.anchor)
            error.return_value = 5
            resolve.side_effect = [self.current, self.original]
            with self.assertRaisesRegex(RuntimeError, "输入已变化"):
                _verify_hidden_input(self.original, self.targets, self.anchor)
        api.kernel.CloseHandle.assert_not_called()
        api.kernel.WriteFile.assert_not_called()

    def test_new_hid_open_success_fails_and_closes_only_its_probe_handle(self):
        api = MagicMock()
        api.kernel.CreateFileW.return_value = 77
        with patch("dsbridge.diagnostics.sony_writer._Api", return_value=api), \
                patch("dsbridge.diagnostics.hot_switch._current_connection", return_value=self.current), \
                patch("dsbridge.diagnostics.hot_switch._physical_path", return_value="current-path"):
            with self.assertRaisesRegex(RuntimeError, "仍能打开当前实体 HID"):
                _verify_hidden_input(self.original, self.targets, self.anchor)
        api.kernel.CloseHandle.assert_called_once_with(77)
        api.kernel.WriteFile.assert_not_called()

    def test_same_observer_keeps_original_reader_and_xinput_until_fresh_recovery(self):
        token = "a" * 32
        phase = {"current": "baseline"}
        phases = iter(["baseline"] + ["bridge"] * 3 + ["verify_hidden"] + ["restored"] * 3 + ["stop"])
        def read_control(path):
            if path == Path("request"):
                return {"format": 1, "token": token, "parent_pid": 123,
                        "connection": {key: value for key, value in self.original.to_dict().items() if key != "label"}}
            phase["current"] = next(phases)
            return {"token": token, "phase": phase["current"]}
        old = SimpleNamespace(error=None, reports=1, read_active=True, close=MagicMock())
        def old_poll():
            if phase["current"] != "baseline":
                old.error = 1167
        old.poll = MagicMock(side_effect=old_poll)
        fresh = SimpleNamespace(error=None, reports=3, read_active=True, poll=MagicMock(), close=MagicMock())
        xi = MagicMock()
        xi.identity.side_effect = lambda slot: ({"vendor_id": 0x37D7, "product_id": 0x2502}
            if slot == 0 and phase["current"] in ("baseline", "restored") else None)
        native = MagicMock()
        native.k.OpenProcess.return_value = 100
        native.k.WaitForSingleObject.return_value = 258
        clock = itertools.count(0, .4)
        hidden = {"device_id": self.current.device_id, "blocked": True, "win32_error": 5,
                  "physical_anchor_verified": True, "no_output": True}
        with patch("dsbridge.diagnostics.hot_switch.sys.frozen", True, create=True), \
                patch("dsbridge.diagnostics.hot_switch._files", return_value=(Path("request"), Path("control"), Path("report"))), \
                patch("dsbridge.diagnostics.hot_switch.read", side_effect=read_control), \
                patch("dsbridge.diagnostics.hot_switch.save_json") as save, \
                patch("dsbridge.diagnostics.hidhide.read_config", return_value={"inverse": False, "allowed_apps": []}), \
                patch("dsbridge.controllers.flydigi.apex6.recovery.Native", return_value=native), \
                patch("dsbridge.platform.windows.xinput.XInput", return_value=xi) as constructor, \
                patch("dsbridge.platform.windows.input_transition_targets.resolve_input_targets", return_value=self.targets), \
                patch("dsbridge.platform.windows.input_transition_targets.capture_input_anchor", return_value=self.anchor), \
                patch("dsbridge.platform.windows.input_transition_targets.resolve_current_input", return_value=self.current_targets), \
                patch("dsbridge.diagnostics.hot_switch._physical_path", side_effect=lambda connection: connection.device_id), \
                patch("dsbridge.diagnostics.hot_switch._Reader", side_effect=[old, fresh]) as reader, \
                patch("dsbridge.diagnostics.hot_switch._verify_hidden_input", return_value=hidden), \
                patch("dsbridge.diagnostics.hot_switch._present_dualsense_count", side_effect=lambda: int(phase["current"] in ("bridge", "verify_hidden"))), \
                patch("dsbridge.diagnostics.hot_switch.time.monotonic", side_effect=lambda: next(clock)), \
                patch("dsbridge.diagnostics.hot_switch.time.sleep"):
            self.assertEqual(observer_main(Path.cwd(), token), 0)
        constructor.assert_called_once()
        self.assertEqual(reader.call_args_list, [call(self.original.device_id, 0x37D7, 0x2502),
                                                call(self.current.device_id, 0x37D7, 0x2502)])
        old.close.assert_called_once()
        fresh.close.assert_called_once()
        xi.close.assert_called_once()
        final = save.call_args.args[1]
        self.assertTrue(final["passed"])
        self.assertEqual(final["original_input_id"], self.original.device_id)
        self.assertEqual(final["fresh_input_id"], self.current.device_id)
        self.assertEqual(final["evidence"]["old_hid_error"], 1167)
        self.assertEqual(final["evidence"]["fresh_hid_reports"], 3)


class NativeReaderTests(unittest.TestCase):
    def reader(self):
        class Overlapped(C.Structure):
            _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                        ("Offset", C.c_uint32), ("OffsetHigh", C.c_uint32), ("hEvent", C.c_void_p)]
        reader = _Reader.__new__(_Reader)
        reader.api = SimpleNamespace(kernel=MagicMock())
        reader.handle, reader.event = 55, 66
        reader.buffer = C.create_string_buffer(4)
        reader.overlapped_type = Overlapped
        reader.overlapped = Overlapped(hEvent=66)
        reader.pending, reader.started, reader.reports, reader.error = True, 1, 0, None
        reader.api.kernel.GetOverlappedResult.return_value = 0
        reader.api.kernel.WaitForSingleObject.return_value = 0
        return reader

    def test_pending_result_never_releases_or_replaces_buffer(self):
        reader = self.reader()
        buffer, overlapped = reader.buffer, reader.overlapped
        with patch("dsbridge.diagnostics.hot_switch.C.get_last_error", return_value=996):
            reader.poll()
        self.assertTrue(reader.pending)
        self.assertIs(reader.buffer, buffer)
        self.assertIs(reader.overlapped, overlapped)
        reader.api.kernel.ReadFile.assert_not_called()

    def test_completion_errors_have_distinct_meanings(self):
        for error in (6, 433, 1167, 995, 31, 1117):
            reader = self.reader()
            with self.subTest(error=error), patch("dsbridge.diagnostics.hot_switch.C.get_last_error", return_value=error):
                if error in (31, 1117):
                    with self.assertRaisesRegex(RuntimeError, str(error)):
                        reader.poll()
                else:
                    reader.poll()
                self.assertFalse(reader.pending)
                self.assertEqual(reader.error, error if error in (6, 433, 1167) else None)

    def test_cancel_completed_closes_owned_handle_and_event_only(self):
        reader = self.reader()
        with patch("dsbridge.diagnostics.hot_switch.C.get_last_error", return_value=995):
            reader.close()
        self.assertFalse(reader.pending)
        self.assertIsNone(reader.handle)
        self.assertIsNone(reader.event)
        self.assertEqual(reader.api.kernel.CloseHandle.call_args_list, [call(55), call(66)])
        reader.api.kernel.CancelIoEx.assert_called_once()

    def test_failed_cancel_retains_memory_and_event_until_worker_exit(self):
        reader = self.reader()
        reader.api.kernel.WaitForSingleObject.return_value = 258
        with patch("dsbridge.diagnostics.hot_switch._PENDING", []) as pending:
            with self.assertRaisesRegex(RuntimeError, "未结束"):
                reader.close()
            self.assertEqual(pending, [reader])
        self.assertTrue(reader.pending)
        self.assertEqual(reader.event, 66)
        reader.api.kernel.CloseHandle.assert_called_once_with(55)

    def test_unfinished_io_exits_without_python_finalization_even_if_report_fails(self):
        for failure in (None, OSError("disk unavailable")):
            with self.subTest(failure=failure), patch("dsbridge.diagnostics.hot_switch._PENDING", [object()]), \
                    patch("dsbridge.diagnostics.hot_switch.save_json", side_effect=failure), \
                    patch("dsbridge.diagnostics.hot_switch.os._exit", side_effect=SystemExit(1)) as exit_process:
                with self.assertRaises(SystemExit):
                    _write_observer_final("unused", {})
                exit_process.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
