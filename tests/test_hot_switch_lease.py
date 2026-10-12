"""Mock-only coverage of visibility ownership, crash ordering and guard lifecycle."""
import copy
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import MagicMock, patch
import uuid

from dsbridge.core.connection import ControllerConnection
from dsbridge.platform.windows import hot_switch_lease as lease
from dsbridge.platform.windows.input_transition_targets import InputTargets
from dsbridge.runtime.paths import RuntimePaths


IDS = (r"HID\VID_37D7&PID_2502&IG_00\INPUT",
       r"USB\VID_37D7&PID_2502&IG_00\GAME",
       r"USB\VID_37D7&PID_2502&MI_00\INTERFACE")
TARGETS = InputTargets(IDS[0], IDS[2], IDS, IDS, "receiver")
CONNECTION = ControllerConnection(kind="receiver", device_id=IDS[0])
TOKEN = "a" * 32
OTHER = r"HID\VID_9999&PID_1111\OTHER"


def anchor_for(targets):
    usb = targets.transport in ("usb", "receiver")
    physical, hub = r"USB\VID_37D7&PID_2502\PHYSICAL", r"USB\ROOT_HUB30\HUB"
    ids = (targets.restart_id, physical, hub) if usb else (targets.restart_id,)
    return dict(format=1, root_id=targets.restart_id, physical_root_id=physical if usb else "",
                hub_id=hub if usb else "", chain=[dict(device_id=value, fingerprint="a" * 64) for value in ids])


def renumbered(group):
    ids = tuple(value.replace("IG_00", "IG_" + group) for value in IDS)
    return InputTargets(ids[0], ids[2], ids, ids, "receiver")


def usb_reply(targets):
    anchor = anchor_for(targets)
    return dict(ok=True, code="restarted", root_id=targets.restart_id, method="usb_hub_cycle_port",
                new_input_id=targets.device_id, new_input_parent_id=targets.device_ids[1],
                physical_root_id=anchor["physical_root_id"], hub_id=anchor["hub_id"], port_index=2)


class FakeDriver:
    def __init__(self, calls):
        self.calls = calls
        self.config = dict(active=True, inverse=False, hidden_devices=[], allowed_nt=[])
        self.before_write = lambda: None
        self.fail_after_hide = False

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        pass

    def snapshot(self):
        return copy.deepcopy(self.config)

    def hidden(self, values):
        self.before_write()
        self.calls.append(("hidden", list(values)))
        self.config["hidden_devices"] = list(values)
        if self.fail_after_hide:
            self.fail_after_hide = False
            raise OSError("lost completion")

    def active(self, value):
        self.before_write()
        self.calls.append(("active", value))
        self.config["active"] = value

    def allow(self, executable):
        self.before_write()
        self.calls.append(("allow", str(executable)))
        self.config["allowed_nt"].append(str(executable))


class FakeNative:
    def __init__(self, *, parent_exit=False):
        self.k = self
        self.parent_alive = True
        self.parent_exit = parent_exit
        self.calls = []
        self.wait_result = 0
        self.on_ready = lambda: None

    def event(self, token, suffix):
        return 11 if suffix == ".ready" else 12

    def claim(self):
        self.calls.append("claim")
        return 30

    def release(self, handle):
        self.calls.append(("release", handle))

    def close(self, handle):
        self.calls.append(("close", handle))

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, inherit, pid))
        return 21

    def WaitForSingleObject(self, handle, timeout):
        return 258 if self.parent_alive else 0

    def WaitForMultipleObjects(self, count, handles, all_handles, timeout):
        if timeout == 0:
            return 258
        if timeout == 0xffffffff:
            self.parent_alive = not self.parent_exit
            return 0 if self.parent_exit else 1
        return self.wait_result

    def SetEvent(self, handle):
        self.calls.append(("set", handle))
        if handle == 11:
            self.on_ready()
        return 1


class HotSwitchLeaseTests(unittest.TestCase):
    def setUp(self):
        # Preserve generated records; the workspace explicitly forbids bulk deletion.
        base = Path(__file__).resolve().parents[1] / "测试记录" / ("热切换恢复-" + uuid.uuid4().hex)
        self.paths = RuntimePaths(base, base).prepare()
        self.calls = []
        self.driver = FakeDriver(self.calls)
        self.client = MagicMock()
        self.client.status.return_value = {"ok": True}
        self.client.restart.side_effect = self.restart
        self.patch("capture_boot_identity", return_value=None)
        self.patch("capture_input_anchor", side_effect=anchor_for)
        self.patch("resolve_current_input", side_effect=lambda targets, anchor: targets)
        self.run_entry = self.patch("recovery_at_logon")
        self.patch("HidHideControl", return_value=self.driver)
        self.claim = patch("dsbridge.controllers.flydigi.apex6.session.ReceiverClaim").start()
        self.addCleanup(patch.stopall)

    def patch(self, name, **kwargs):
        target = lease
        *parents, attribute = name.split(".")
        for parent in parents:
            target = getattr(target, parent)
        return patch.object(target, attribute, **kwargs).start()

    def read(self, filename=lease.TRANSACTION):
        return json.loads((self.paths.state / filename).read_text(encoding="utf-8"))

    def restart(self, device_id, check_cancel=lambda: None):
        check_cancel()
        self.calls.append(("restart", device_id))
        return {"ok": True, "code": "restarted", "root_id": TARGETS.restart_id}

    def hide(self, targets=TARGETS):
        return lease._begin_hiding(self.paths, TOKEN, 123, targets)

    def mark_refresh_needed(self):
        record = self.read()
        record["restart_needed"] = True
        lease.save_json(self.paths.state / lease.TRANSACTION, record)

    def restore(self):
        return lease._restore_transaction(self.paths, self.client)

    def request(self, targets=TARGETS):
        lease.save_json(self.paths.state / ("hot-switch-request-" + TOKEN + ".json"),
                        dict(format=1, token=TOKEN, parent_pid=123, connection=CONNECTION.to_dict(), targets=targets.to_dict()))

    def test_complete_restore_plan_and_run_entry_precede_every_first_write(self):
        def before():
            tx = self.read()
            journal = self.read(lease.legacy.JOURNAL)
            self.assertTrue(tx["restore_pending"])
            self.assertEqual(tx["added_devices"], list(IDS))
            self.assertEqual(journal["token"], TOKEN)
            self.assertTrue(journal["hot_switch"])
            self.run_entry.assert_called_with(self.paths, True)
        self.driver.before_write = before
        self.hide()

    def test_unhide_merges_only_owned_and_refreshes_after_visibility(self):
        self.driver.config["hidden_devices"] = [OTHER, IDS[0].lower()]
        record = self.hide()
        self.assertEqual(record["added_devices"], list(IDS[1:]))
        self.mark_refresh_needed()
        self.driver.config["hidden_devices"] += ["NEW_OTHER", IDS[1].lower()]
        self.calls.clear()
        result = self.restore()
        self.assertTrue(result["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [OTHER, IDS[0].lower(), "NEW_OTHER"])
        self.assertEqual(self.calls[-1], ("restart", IDS[0]))
        self.assertFalse(self.read(lease.legacy.JOURNAL)["restore_pending"])
        self.assertFalse(self.read()["restore_pending"])
        self.run_entry.assert_called_with(self.paths, False)

    def test_already_hidden_targets_are_not_owned_or_removed(self):
        self.driver.config["hidden_devices"] = list(IDS)
        self.assertEqual(self.hide()["added_devices"], [])
        self.mark_refresh_needed()
        self.assertTrue(self.restore()["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], list(IDS))

    def test_restore_original_inactive_with_preexisting_selected_hide(self):
        self.driver.config.update(active=False, hidden_devices=[IDS[0]])
        self.hide()
        self.assertTrue(self.driver.config["active"])
        self.restore()
        self.assertFalse(self.driver.config["active"])
        self.assertEqual(self.driver.config["hidden_devices"], [IDS[0]])

    def test_new_other_hidden_item_prevents_disabling_global_hiding(self):
        self.driver.config["active"] = False
        self.hide()
        self.driver.config["hidden_devices"].append(OTHER)
        self.restore()
        self.assertTrue(self.driver.config["active"])
        self.assertEqual(self.driver.config["hidden_devices"], [OTHER])

    def test_inactive_other_targets_and_inverse_fail_before_writes(self):
        for inverse in (False, True):
            with self.subTest(inverse=inverse):
                self.driver.config.update(active=False, inverse=inverse, hidden_devices=[OTHER])
                before = self.driver.snapshot()
                with self.assertRaises(RuntimeError):
                    self.hide()
                self.assertEqual(self.driver.snapshot(), before)
                self.assertFalse((self.paths.state / lease.TRANSACTION).exists())
                self.assertEqual(self.calls, [])

    def test_lost_hide_ack_is_recovered_from_prepared_transaction(self):
        self.driver.fail_after_hide = True
        with self.assertRaisesRegex(OSError, "lost completion"):
            self.hide()
        self.assertEqual(self.read()["phase"], "prepared")
        self.assertTrue(self.restore()["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.client.restart.assert_not_called()

    def test_run_entry_failure_prevents_all_visibility_writes(self):
        self.run_entry.side_effect = OSError("registry unavailable")
        with self.assertRaises(OSError):
            self.hide()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.read()["restore_pending"])

    def test_refresh_failure_unhides_offline_device_and_keeps_retry_journal(self):
        self.hide()
        self.mark_refresh_needed()
        self.client.restart.side_effect = RuntimeError("device absent")
        result = self.restore()
        self.assertFalse(result["restored"])
        self.assertTrue(result["input_visibility_restored"])
        self.assertTrue(result["refresh_pending"])
        self.assertEqual(result["refresh_error_details"], {})
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.assertFalse(self.read(lease.legacy.JOURNAL)["restore_pending"])
        self.assertEqual(self.read()["phase"], "refresh_pending")
        self.run_entry.assert_called_with(self.paths, True)
        self.client.restart.side_effect = self.restart
        self.assertTrue(self.restore()["restored"])
        self.assertNotIn("refresh_error", self.read())
        self.assertNotIn("refresh_error_details", self.read())

    def test_refresh_failure_preserves_native_details_and_retry_clears_them(self):
        self.hide()
        self.mark_refresh_needed()
        error = RuntimeError("需要重启 Windows")
        error.details = dict(disable_result=0, enable_result=0,
                             status=0x180210a, problem=0, restart_required=True,
                             status_before=0x180210a, problem_before=0)
        expected = dict(error.details)
        self.client.restart.side_effect = error
        result = self.restore()
        self.assertFalse(result["restored"])
        self.assertEqual(result["refresh_error"], str(error))
        self.assertEqual(result["refresh_error_details"], expected)
        self.assertEqual(self.read()["refresh_error_details"], expected)
        error.details["status"] = 0
        self.assertEqual(result["refresh_error_details"], expected)
        self.client.restart.side_effect = self.restart
        self.assertTrue(self.restore()["restored"])
        self.assertNotIn("refresh_error", self.read())
        self.assertNotIn("refresh_error_details", self.read())

    def mock_later_boot(self):
        identity = {"captured": "before"}
        evidence = {"boot_before": identity, "boot_after": {"captured": "after"}}
        self.patch("prove_later_boot", return_value=None)
        self.patch("capture_boot_identity", return_value=identity)
        self.hide()
        self.mark_refresh_needed()
        self.assertEqual(self.read()["boot_identity"], identity)
        # The previous boot restored visibility but could not refresh old
        # consumers; only a subsequent full reboot can discharge that refresh.
        self.client.restart.side_effect = RuntimeError("previous boot refresh failed")
        self.assertFalse(self.restore()["restored"])
        self.assertEqual(self.read()["visibility_restored_boot_identity"], identity)
        self.client.restart.side_effect = self.restart
        self.client.restart.reset_mock()
        self.patch("capture_boot_identity", return_value=evidence["boot_after"])
        self.patch("prove_later_boot", side_effect=lambda saved: evidence if saved == identity else None)
        self.client.status.return_value = dict(ok=True, restart_required=False, recovery_pending=False)
        self.patch("_read_reboot_input", return_value=dict(root_id=IDS[2], status=0x180200a,
                                                         problem=0, input_readable=True))
        self.patch("_check_reboot_outputs")
        return evidence

    def test_confirmed_full_reboot_finishes_after_readback_without_another_cycle(self):
        evidence = self.mock_later_boot()
        result = lease._restore_transaction(self.paths, self.client, outputs_restored=True)
        self.assertTrue(result["restored"])
        self.assertEqual(result["refresh_result"]["method"], "system_reboot")
        self.assertEqual(result["refresh_result"]["boot_after"], evidence["boot_after"])
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.assertFalse(self.read()["restart_needed"])
        self.assertFalse(self.read()["restore_pending"])
        self.client.restart.assert_not_called()
        self.run_entry.assert_called_with(self.paths, False)

    def test_known_reboot_with_unverified_outputs_retains_pending_without_cycle(self):
        self.mock_later_boot()
        result = self.restore()
        self.assertFalse(result["restored"])
        self.assertTrue(result["input_visibility_restored"])
        self.client.status.assert_not_called()
        self.client.restart.assert_not_called()
        self.run_entry.assert_called_with(self.paths, True)

    def test_known_reboot_requires_clear_native_recovery_and_healthy_selected_input(self):
        self.mock_later_boot()
        for service in (dict(ok=True, restart_required=False),
                        dict(ok=True, restart_required=True, recovery_pending=False),
                        dict(ok=True, restart_required=False, recovery_pending=True)):
            with self.subTest(service=service):
                self.client.status.return_value = service
                result = lease._restore_transaction(self.paths, self.client, outputs_restored=True)
                self.assertFalse(result["restored"])
                self.client.restart.assert_not_called()
        self.client.status.return_value = dict(ok=True, restart_required=False, recovery_pending=False)
        self.patch("_read_reboot_input", side_effect=RuntimeError("input not readable"))
        result = lease._restore_transaction(self.paths, self.client, outputs_restored=True)
        self.assertFalse(result["restored"])
        self.client.restart.assert_not_called()

    def test_known_reboot_does_not_finish_while_audio_or_motor_restore_is_pending(self):
        self.mock_later_boot()
        self.patch("_check_reboot_outputs", side_effect=RuntimeError("audio still Sony"))
        result = lease._restore_transaction(self.paths, self.client, outputs_restored=True)
        self.assertFalse(result["restored"])
        self.assertIn("audio still Sony", result["refresh_error"])
        self.client.restart.assert_not_called()

    def test_hidden_until_after_reboot_still_needs_normal_refresh(self):
        self.mock_later_boot()
        # Visibility changed again after its recorded restoration. A reboot
        # that preceded this unhide cannot discharge the new refresh need.
        self.driver.config["hidden_devices"] = list(IDS)
        result = lease._restore_transaction(self.paths, self.client, outputs_restored=True)
        self.assertTrue(result["restored"])
        self.assertNotEqual(result["refresh_result"].get("method"), "system_reboot")
        self.assertEqual(self.read()["visibility_restored_boot_identity"], {"captured": "after"})
        self.client.restart.assert_called_once()

    def test_transaction_boot_alone_cannot_discharge_visibility_recovery(self):
        self.mock_later_boot()
        record = self.read()
        record.pop("visibility_restored_boot_identity")
        lease.save_json(self.paths.state / lease.TRANSACTION, record)
        result = lease._restore_transaction(self.paths, self.client, outputs_restored=True)
        self.assertTrue(result["restored"])
        self.client.restart.assert_called_once()

    def test_legacy_missing_boot_identity_still_uses_normal_refresh(self):
        self.hide()
        self.mark_refresh_needed()
        saved = self.read()
        saved.pop("boot_identity")
        lease.save_json(self.paths.state / lease.TRANSACTION, saved)
        self.assertTrue(lease._restore_transaction(self.paths, self.client, outputs_restored=True)["restored"])
        self.client.restart.assert_called_once()

    def test_boot_probe_failure_does_not_prevent_normal_owned_hiding(self):
        self.patch("capture_boot_identity", side_effect=OSError("event log unavailable"))
        self.hide()
        self.assertIsNone(self.read()["boot_identity"])
        self.assertEqual(self.driver.config["hidden_devices"], list(IDS))

    def test_new_boot_can_retire_reused_parent_pid_but_not_live_export_owner(self):
        self.mock_later_boot()
        self.patch("_recover_outputs", return_value=[])
        self.assertTrue(lease._recover_locked(self.paths, FakeNative(), self.client)["restored"])
        self.client.restart.assert_not_called()
        self.mock_later_boot()
        lease.save_json(self.paths.state / "viiper-usb-session.json",
                        dict(format=1, restore_pending=True, process_id=123))
        with self.assertRaisesRegex(RuntimeError, "管理虚拟 DS"):
            lease._recover_locked(self.paths, FakeNative(), self.client)

    def test_reboot_input_readback_rejects_pnp_problem_restart_and_missing_state(self):
        self.patch("resolve_input_targets", return_value=TARGETS)
        cfg = MagicMock()
        cfg.CM_Locate_DevNodeW.return_value = 0
        status_value = 0x180200a
        def status(status_pointer, problem_pointer, node, flags):
            status_pointer._obj.value = status_value
            problem_pointer._obj.value = 0
            return 0
        cfg.CM_Get_DevNode_Status.side_effect = status
        self.patch("C.WinDLL", return_value=cfg)
        source = MagicMock()
        source.rebind.return_value = 0
        source.get_state.return_value = {"buttons": 0}
        with patch("dsbridge.platform.windows.xinput.XInput", return_value=source):
            for status_value in (0x180240a, 0x180210a, 0x1802002):
                with self.subTest(status=status_value), self.assertRaisesRegex(RuntimeError, "尚未正常启动"):
                    lease._read_reboot_input(TARGETS)
            source.rebind.assert_not_called()
            status_value = 0x180200a
            health = lease._read_reboot_input(TARGETS)
            self.assertTrue(health["input_readable"])
            self.assertNotIn("buttons", health)
            self.assertEqual(source.rebind.call_args.args[0].vendor_id, 0x37d7)
            source.get_state.return_value = None
            with self.assertRaisesRegex(RuntimeError, "尚未恢复普通输入"):
                lease._read_reboot_input(TARGETS)
            self.assertEqual(source.close.call_count, 2)

    def test_reboot_input_readback_refuses_changed_exact_target_before_native_check(self):
        self.patch("resolve_input_targets", return_value=InputTargets(IDS[0], "OTHER", IDS, IDS, "receiver"))
        native = self.patch("C.WinDLL")
        with self.assertRaisesRegex(RuntimeError, "分支与恢复记录不一致"):
            lease._read_reboot_input(TARGETS)
        native.assert_not_called()

    def test_wrong_service_root_cannot_report_restored(self):
        self.hide()
        self.mark_refresh_needed()
        self.client.restart.side_effect = None
        self.client.restart.return_value = {"ok": True, "code": "restarted", "root_id": "USB\\RADIO"}
        result = self.restore()
        self.assertFalse(result["restored"])
        self.assertTrue(result["refresh_pending"])

    def test_restore_readback_failure_retains_ownership_and_does_not_refresh(self):
        self.hide()
        self.mark_refresh_needed()
        self.driver.hidden = lambda values: None
        with self.assertRaisesRegex(RuntimeError, "尚未解除"):
            self.restore()
        self.assertTrue(self.read()["restore_pending"])
        self.client.restart.assert_not_called()
        self.run_entry.assert_called_with(self.paths, True)

    def test_completed_transaction_removes_leftover_run_entry(self):
        self.hide()
        self.restore()
        self.run_entry.reset_mock()
        self.assertTrue(self.restore()["restored"])
        self.run_entry.assert_called_once_with(self.paths, False)

    def test_missing_visibility_journal_still_restores_from_transaction(self):
        self.hide()
        # Simulate a crash between durable transaction and journal writes without deleting a file.
        original_read = lease._read
        self.patch("_read", side_effect=lambda path: None if Path(path).name == lease.legacy.JOURNAL else original_read(path))
        self.assertTrue(self.restore()["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])

    def test_crash_between_new_transaction_and_compatibility_journal_is_recoverable(self):
        old = dict(format=1, token="b" * 32, restore_pending=False, restored_at="previous")
        lease.save_json(self.paths.state / lease.legacy.JOURNAL, old)
        real_save = lease.save_json
        def save(path, value):
            if Path(path).name == lease.legacy.JOURNAL:
                raise OSError("crash after transaction")
            return real_save(path, value)
        with patch.object(lease, "save_json", side_effect=save):
            with self.assertRaises(OSError):
                self.hide()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.restore()["restored"])
        self.assertEqual(self.read(lease.legacy.JOURNAL), old)

    def test_replaced_journal_and_forged_scope_never_change_visibility(self):
        self.hide()
        original = self.read()
        journal = self.read(lease.legacy.JOURNAL)
        journal["token"] = "b" * 32
        lease.save_json(self.paths.state / lease.legacy.JOURNAL, journal)
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "替换"):
            self.restore()
        self.assertEqual(self.calls, [])
        for changes in ({"device_id": []}, {"added_devices": [OTHER]},
                        {"targets": {**original["targets"], "restart_id": "USB\\RADIO"}},
                        {"restart_needed": True, "restore_pending": False}):
            with self.subTest(changes=changes):
                lease.save_json(self.paths.state / lease.TRANSACTION, {**original, **changes})
                with self.assertRaises(RuntimeError):
                    self.restore()
                self.assertEqual(self.calls, [])

    def test_bluetooth_journal_hides_only_selected_profile_and_hid(self):
        service = "{00001124-0000-1000-8000-00805F9B34FB}"
        hid = "HID\\" + service + "&VID_045E&PID_02E0&IG_00\\INPUT"
        profile = "BTHENUM\\" + service + "_VID&0002045E_PID&02E0\\PROFILE"
        targets = InputTargets(hid, profile, (hid, profile), (hid, profile), "bluetooth")
        self.assertEqual(lease._saved_targets(targets.to_dict()), targets)
        self.hide(targets)
        self.assertTrue((self.paths.state / lease.BLUETOOTH_JOURNAL).is_file())
        self.assertFalse((self.paths.state / lease.legacy.JOURNAL).exists())
        self.restore()
        self.assertEqual(self.driver.config["hidden_devices"], [])

    def test_live_legacy_owner_blocks_output_recovery_before_any_cleanup(self):
        lease.save_json(self.paths.state / lease.legacy.JOURNAL,
                        dict(format=1, restore_pending=True, parent_pid=999))
        outputs = self.patch("_recover_outputs")
        with self.assertRaisesRegex(RuntimeError, "仍在运行"):
            lease._recover_locked(self.paths, FakeNative(), self.client)
        outputs.assert_not_called()
        self.claim.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_live_new_owner_blocks_output_recovery(self):
        self.hide()
        outputs = self.patch("_recover_outputs")
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "仍在运行"):
            lease._recover_locked(self.paths, FakeNative(), self.client)
        outputs.assert_not_called()
        self.claim.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_finished_exited_guard_allows_recovery_while_gui_is_still_open(self):
        self.hide()
        self.mark_refresh_needed()
        lease.save_json(self.paths.state / ("input-guard-" + TOKEN + ".json"),
                        dict(token=TOKEN, parent_pid=123, guard_pid=456, phase="finished", restored=False))
        self.patch("_parent_is_live", side_effect=lambda native, pid: pid == 123)
        self.patch("_recover_outputs", return_value=[])
        result = lease._recover_locked(self.paths, FakeNative(), self.client)
        self.assertTrue(result["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.client.restart.assert_called_once()

    def test_unfinished_mismatched_or_live_guard_cannot_retire_gui_ownership(self):
        self.hide()
        outputs = self.patch("_recover_outputs")
        live = self.patch("_parent_is_live", side_effect=lambda native, pid: pid == 123)
        original = dict(token=TOKEN, parent_pid=123, guard_pid=456, phase="finished")
        for changes in ({"phase": "ready"}, {"token": "b" * 32}, {"parent_pid": 999},
                        {"guard_pid": 123}, {"guard_pid": "456"}, {"guard_pid": 0}):
            with self.subTest(changes=changes):
                lease.save_json(self.paths.state / ("input-guard-" + TOKEN + ".json"), {**original, **changes})
                with self.assertRaisesRegex(RuntimeError, "仍在运行"):
                    lease._recover_locked(self.paths, FakeNative(), self.client)
                outputs.assert_not_called()
        lease.save_json(self.paths.state / ("input-guard-" + TOKEN + ".json"), original)
        live.side_effect = None
        live.return_value = True
        with self.assertRaisesRegex(RuntimeError, "仍在运行"):
            lease._recover_locked(self.paths, FakeNative(), self.client)
        outputs.assert_not_called()

    def test_finished_input_guard_does_not_waive_live_exporter_protection(self):
        self.hide()
        lease.save_json(self.paths.state / ("input-guard-" + TOKEN + ".json"),
                        dict(token=TOKEN, parent_pid=123, guard_pid=456, phase="finished", restored=False))
        lease.save_json(self.paths.state / "viiper-usb-session.json",
                        dict(format=1, restore_pending=True, process_id=123))
        self.patch("_parent_is_live", side_effect=lambda native, pid: pid == 123)
        outputs = self.patch("_recover_outputs")
        with self.assertRaisesRegex(RuntimeError, "管理虚拟 DS"):
            lease._recover_locked(self.paths, FakeNative(), self.client)
        outputs.assert_not_called()
        self.assertTrue(self.read()["restore_pending"])

    def test_live_legacy_bluetooth_owner_blocks_recovery_without_receiver_journal(self):
        lease.save_json(self.paths.state / "viiper-usb-session.json",
                        dict(format=1, restore_pending=True, process_id=888))
        outputs = self.patch("_recover_outputs")
        with self.assertRaisesRegex(RuntimeError, "管理虚拟 DS"):
            lease._recover_locked(self.paths, FakeNative(), self.client)
        outputs.assert_not_called()
        self.claim.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_parent_exit_does_not_clean_output_journal_replaced_by_live_owner(self):
        lease.save_json(self.paths.state / "viiper-usb-session.json",
                        dict(format=1, restore_pending=True, process_id=888))
        self.patch("Native", return_value=FakeNative())
        load_module = self.patch("importlib.import_module")
        warnings = lease._recover_outputs(self.paths)
        self.assertIn("管理虚拟 DS", warnings[0])
        load_module.assert_not_called()

    def test_unknown_output_owner_fails_closed_without_recovery(self):
        for process_id in (None, False, -1):
            with self.subTest(process_id=process_id):
                lease.save_json(self.paths.state / "viiper-usb-session.json",
                                dict(format=1, restore_pending=True, process_id=process_id))
                outputs = self.patch("_recover_outputs")
                with self.assertRaisesRegex(RuntimeError, "进程身份"):
                    lease._recover_locked(self.paths, FakeNative(), self.client)
                outputs.assert_not_called()

    def test_missing_transaction_for_pending_hot_journal_fails_closed(self):
        lease.save_json(self.paths.state / lease.legacy.JOURNAL,
                        dict(format=1, restore_pending=True, parent_pid=999, hot_switch=True))
        outputs = self.patch("_recover_outputs")
        with self.assertRaisesRegex(RuntimeError, "缺少热切换"):
            lease._recover_locked(self.paths, FakeNative(), self.client)
        outputs.assert_not_called()

    def test_output_import_or_repair_failure_does_not_skip_later_cleanup(self):
        success = MagicMock()
        modules = [ImportError("missing USB module"),
                   types.SimpleNamespace(recover_pending_motors=MagicMock(side_effect=RuntimeError("offline motors"))),
                   types.SimpleNamespace(repair_audio_defaults=success)]
        self.patch("importlib.import_module", side_effect=modules)
        warnings = lease._recover_outputs(self.paths)
        self.assertEqual(len(warnings), 2)
        success.assert_called_once_with(self.paths)

    def test_guard_revalidates_request_in_live_tree(self):
        self.request()
        changed = InputTargets(IDS[0], IDS[2] + "X", (*IDS[:2], IDS[2] + "X"), (*IDS[:2], IDS[2] + "X"), "receiver")
        self.patch("resolve_input_targets", return_value=changed)
        with self.assertRaisesRegex(RuntimeError, "已经改变"):
            lease._request_targets(self.paths, TOKEN, 123)
        self.assertEqual(self.calls, [])

    def run_guard(self, *, parent_exit=False, fail_restart=False):
        self.request()
        native = FakeNative(parent_exit=parent_exit)
        self.patch("Native", return_value=native)
        self.patch("HotSwitchClient", return_value=self.client)
        self.patch("resolve_input_targets", return_value=TARGETS)
        self.patch("_recover_locked", return_value={"restored": True})
        self.patch("_recover_outputs", side_effect=lambda paths: self.calls.append(("outputs",)) or [])
        ready_reports = []
        native.on_ready = lambda: ready_reports.append(self.read("input-guard-" + TOKEN + ".json"))
        attempts = 0
        def restart(device_id, check_cancel=lambda: None):
            nonlocal attempts
            attempts += 1
            record = self.read()
            self.assertTrue(record["restart_needed"])
            if attempts == 1:
                self.assertEqual(record["phase"], "switching")
                self.assertEqual(self.driver.config["hidden_devices"], list(IDS))
                if fail_restart:
                    if isinstance(fail_restart, Exception):
                        raise fail_restart
                    raise RuntimeError("restart rejected")
            return self.restart(device_id, check_cancel)
        self.client.restart.side_effect = restart
        result = lease.guard_main(self.paths, TOKEN, 123)
        return result, native, ready_reports, self.read("input-guard-" + TOKEN + ".json")

    def test_ready_requires_restart_then_normal_stop_restores_and_restarts(self):
        result, native, reports, report = self.run_guard()
        self.assertEqual(result, 0)
        self.assertEqual([r["phase"] for r in reports], ["ready", "finished"])
        self.assertTrue(report["restored"])
        self.assertEqual(report["reason"], "normal_stop")
        self.assertEqual(self.client.restart.call_count, 2)
        self.assertIn(("OpenProcess", 0x100000, False, 123), native.calls)
        self.assertEqual(self.driver.config["hidden_devices"], [])
        isolation = reports[0]["isolation"]
        self.assertTrue(isolation["converged"])
        self.assertEqual(isolation["rounds"], 1)
        self.assertEqual(isolation["hidden_before_last_refresh"], list(IDS))
        self.assertEqual(isolation["final_hidden_ids"], list(IDS))
        self.assertTrue(isolation["evidence"][0]["native_success"])
        self.assertTrue(isolation["evidence"][0]["verified"])

    def test_parent_crash_restores_output_before_physical_visibility(self):
        result, _, _, report = self.run_guard(parent_exit=True)
        self.assertEqual(result, 0)
        self.assertEqual(report["reason"], "parent_exit")
        self.assertLess(self.calls.index(("outputs",)), self.calls.index(("hidden", [])))
        self.assertTrue(report["input_visibility_restored"])

    def test_failed_restart_never_announces_ready_and_still_restores(self):
        result, _, reports, report = self.run_guard(fail_restart=True)
        self.assertEqual(result, 1)
        self.assertEqual([r["phase"] for r in reports], ["finished"])
        self.assertIn("restart rejected", report["error"])
        self.assertEqual(report["error_details"], {})
        self.assertTrue(report["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.assertEqual(report["isolation"]["rounds"], 1)
        self.assertFalse(report["isolation"]["evidence"][0]["native_success"])
        self.assertEqual(self.client.restart.call_count, 2)  # failed startup, then visibility restoration only

    def test_guard_start_failure_keeps_native_details_after_successful_cleanup(self):
        error = RuntimeError("需要重启 Windows")
        error.details = dict(disable_result=0, enable_result=0,
                             status=0x180210a, problem=0, restart_required=True)
        result, _, reports, report = self.run_guard(fail_restart=error)
        self.assertEqual(result, 1)
        self.assertEqual([r["phase"] for r in reports], ["finished"])
        self.assertEqual(report["error"], str(error))
        self.assertEqual(report["error_details"], error.details)
        self.assertNotIn("refresh_error_details", report)
        self.assertTrue(report["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])

    def run_renumbering_guard(self, *, lost_hide_ack=False, never_converges=False, fail_round=None):
        self.request()
        live = {"targets": TARGETS}
        self.patch("resolve_current_input", side_effect=lambda targets, anchor: live["targets"])
        self.patch("resolve_input_targets", return_value=TARGETS)
        self.patch("_recover_locked", return_value={"restored": True})
        self.patch("HotSwitchClient", return_value=self.client)
        native = FakeNative()
        self.patch("Native", return_value=native)
        ready = []
        native.on_ready = lambda: ready.append((self.read("input-guard-" + TOKEN + ".json"), self.driver.snapshot()))
        calls = []
        self.switch_records = []
        def restart(device_id, check_cancel=lambda: None):
            self.assertEqual(device_id, live["targets"].device_id)
            calls.append(device_id)
            record = self.read()
            if record["phase"] == "switching":
                self.switch_records.append(record)
                evidence = record["isolation"]["evidence"][-1]
                self.assertFalse(evidence["native_success"])
                self.assertEqual(evidence["pre_hidden"], self.driver.config["hidden_devices"])
                self.assertEqual(evidence["current"], [])
                if len(self.switch_records) == fail_round:
                    raise TimeoutError("native response timeout")
                live["targets"] = renumbered("02" if never_converges and len(self.switch_records) == 2 else "01")
            else:
                live["targets"] = renumbered("03" if never_converges else "02")
            if lost_hide_ack and len(calls) == 1:
                self.driver.fail_after_hide = True
            return usb_reply(live["targets"])
        self.client.restart.side_effect = restart
        def before_write():
            record = self.read()
            if record["current_targets"]["device_id"] == renumbered("01").device_id and not record["input_visibility_restored"]:
                self.assertTrue(set(renumbered("01").hidden_ids).issubset(record["added_devices"]))
                self.assertEqual(self.read(lease.legacy.JOURNAL)["added_devices"], record["added_devices"])
        self.driver.before_write = before_write
        result = lease.guard_main(self.paths, TOKEN, 123)
        return result, calls, ready, self.read("input-guard-" + TOKEN + ".json")

    def test_switch_and_stop_track_two_ig_changes_and_all_owned_hiding(self):
        result, calls, reports, report = self.run_renumbering_guard()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [TARGETS.device_id, renumbered("01").device_id, renumbered("01").device_id])
        self.assertEqual([entry[0]["phase"] for entry in reports], ["ready", "finished"])
        ready, hidden = reports[0]
        self.assertEqual(ready["targets"], TARGETS.to_dict())
        self.assertEqual(ready["current_targets"], renumbered("01").to_dict())
        self.assertTrue(set(renumbered("01").hidden_ids).issubset(hidden["hidden_devices"]))
        self.assertEqual(report["current_targets"], renumbered("02").to_dict())
        self.assertTrue(report["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.assertEqual(len(self.read()["target_history"]), 3)
        self.assertEqual(len(self.read()["added_devices"]), 5)
        self.assertEqual(lease._load_transaction(self.paths)["targets"], TARGETS.to_dict())
        isolation = ready["isolation"]
        self.assertTrue(isolation["converged"])
        self.assertEqual(isolation["rounds"], 2)
        self.assertEqual([entry["verified"] for entry in isolation["evidence"]], [False, True])
        self.assertEqual(isolation["evidence"][0]["pre_hidden"], list(IDS))
        self.assertNotIn(renumbered("01").device_id, isolation["evidence"][0]["pre_hidden"])
        self.assertEqual(isolation["final_input_id"], renumbered("01").device_id)
        self.assertTrue(set(isolation["final_hidden_ids"]).issubset(isolation["hidden_before_last_refresh"]))
        self.assertEqual(report["isolation"], isolation)  # stop's IG_02 must not replace ready evidence

    def test_second_new_generation_refuses_ready_and_restores_every_owned_id(self):
        result, calls, reports, report = self.run_renumbering_guard(never_converges=True)
        self.assertEqual(result, 1)
        self.assertEqual([entry[0]["phase"] for entry in reports], ["finished"])
        self.assertIn("自动切换未完成", report["error"])
        self.assertEqual(len(self.switch_records), 2)
        self.assertEqual(len(calls), 3)  # exactly two startup cycles and one explicit restoration
        self.assertFalse(report["isolation"]["converged"])
        self.assertEqual([entry["verified"] for entry in report["isolation"]["evidence"]], [False, False])
        self.assertTrue(report["restored"])
        self.assertEqual(self.driver.config["hidden_devices"], [])
        self.assertEqual(len(self.read()["added_devices"]), 7)

    def test_native_timeout_has_no_second_startup_attempt(self):
        for number in (1, 2):
            with self.subTest(number=number):
                result, calls, reports, report = self.run_renumbering_guard(fail_round=number)
                self.assertEqual(result, 1)
                self.assertEqual([entry[0]["phase"] for entry in reports], ["finished"])
                self.assertEqual(len(self.switch_records), number)
                self.assertEqual(len(calls), number + 1)  # restoration happens only after unhiding
                self.assertFalse(report["isolation"]["evidence"][-1]["native_success"])
                self.assertTrue(report["restored"])
                self.assertEqual(self.driver.config["hidden_devices"], [])

    def test_hidden_state_change_during_successful_refresh_refuses_convergence(self):
        record = self.hide()
        def restart(*unused):
            self.driver.config["inverse"] = True
            return usb_reply(TARGETS)
        self.client.restart.side_effect = restart
        with self.assertRaisesRegex(RuntimeError, "刷新期间"):
            lease._switch_isolated(self.paths, self.client, record)
        self.client.restart.assert_called_once()
        self.assertFalse(self.read()["isolation"]["converged"])

    def test_pre_refresh_snapshot_excludes_unrelated_devices(self):
        self.driver.config["hidden_devices"] = [OTHER]
        record = self.hide()
        lease._switch_isolated(self.paths, self.client, record)
        self.assertNotIn(OTHER, record["isolation"]["hidden_before_last_refresh"])
        self.assertTrue(record["isolation"]["converged"])

    def test_lost_new_instance_hide_ack_restores_both_generations(self):
        result, calls, reports, report = self.run_renumbering_guard(lost_hide_ack=True)
        self.assertEqual(result, 1)
        self.assertEqual([entry[0]["phase"] for entry in reports], ["finished"])
        self.assertIn("lost completion", report["error"])
        self.assertTrue(report["restored"])
        self.assertEqual(calls[-1], renumbered("01").device_id)
        self.assertEqual(self.driver.config["hidden_devices"], [])

    def test_legacy_pending_uses_protected_alias_then_verifies_current_physical_path(self):
        self.hide()
        self.mark_refresh_needed()
        record = self.read()
        for key in ("input_anchor", "current_targets", "target_history"):
            record.pop(key)
        lease.save_json(self.paths.state / lease.TRANSACTION, record)
        current = renumbered("01")
        self.patch("resolve_input_targets", return_value=current)
        self.client.restart.side_effect = None
        self.client.restart.return_value = usb_reply(current)
        result = self.restore()
        self.assertTrue(result["restored"])
        self.client.restart.assert_called_once_with(TARGETS.device_id, unittest.mock.ANY)
        self.assertEqual(self.read()["current_targets"], current.to_dict())
        self.assertEqual(self.read()["input_anchor"], anchor_for(current))
        lease._load_transaction(self.paths)

    def test_service_cannot_move_current_input_to_another_physical_root_hub_or_port(self):
        self.hide()
        current = renumbered("01")
        self.patch("resolve_current_input", return_value=current)
        for changes in ({"physical_root_id": r"USB\VID_37D7&PID_2502\OTHER"},
                        {"hub_id": r"USB\ROOT_HUB30\OTHER"}, {"port_index": True},
                        {"new_input_parent_id": IDS[1]}, {"new_input_id": IDS[0]}):
            with self.subTest(changes=changes):
                record = self.read()
                self.calls.clear()
                with self.assertRaises(RuntimeError):
                    lease._adopt_restart_reply(self.paths, record, {**usb_reply(current), **changes}, hide_new=True)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.read()["current_targets"], TARGETS.to_dict())
        record = self.read()
        record["usb_port_index"] = 3
        with self.assertRaisesRegex(RuntimeError, "端口或输入身份"):
            lease._adopt_restart_reply(self.paths, record, usb_reply(current))

    def test_parent_current_connection_preserves_controller_metadata(self):
        instance, _, process = self.make_parent_lease()
        current = renumbered("01")
        def launch(*args, **kwargs):
            lease.save_json(instance.report_path, dict(token=instance.token, phase="ready", targets=TARGETS.to_dict(),
                current_targets=current.to_dict(), input_anchor=anchor_for(current)))
            return process
        self.patch("subprocess.Popen", side_effect=launch)
        self.patch("resolve_current_input", return_value=current)
        instance.start_cancellable(lambda: None)
        self.assertEqual(instance.current_connection.device_id, current.device_id)
        self.assertEqual(instance.current_connection.kind, CONNECTION.kind)
        self.assertEqual(instance.connection.device_id, TARGETS.device_id)

    def make_parent_lease(self):
        native = FakeNative()
        self.patch("Native", return_value=native)
        instance = lease.HotSwitchLease(self.paths, CONNECTION, TARGETS)
        process = MagicMock()
        process._handle = 50
        process.poll.return_value = None
        def launch(*args, **kwargs):
            lease.save_json(instance.report_path, dict(token=instance.token, phase="ready", targets=TARGETS.to_dict()))
            return process
        self.patch("subprocess.Popen", side_effect=launch)
        return instance, native, process

    def test_parent_start_passes_exact_request_and_waits_for_ready(self):
        instance, _, process = self.make_parent_lease()
        instance.start_cancellable(lambda: None)
        request = self.read("hot-switch-request-" + instance.token + ".json")
        self.assertEqual(request["targets"], TARGETS.to_dict())
        self.assertIsNone(instance.error)
        process.poll.return_value = 0
        self.assertIsInstance(instance.error, RuntimeError)

    def test_stop_timeout_retains_guard_and_events_without_killing_any_process(self):
        instance, native, process = self.make_parent_lease()
        instance.start_cancellable(lambda: None)
        process.wait.side_effect = subprocess.TimeoutExpired("guard", 45)
        with self.assertRaises(subprocess.TimeoutExpired):
            instance.stop()
        self.assertIs(instance.process, process)
        self.assertEqual(instance.stop_event, 12)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertNotIn(("close", 12), native.calls)
        lease.save_json(instance.report_path, dict(token=instance.token, phase="finished", restored=True))
        process.wait.side_effect = None
        instance.stop()
        self.assertIsNone(instance.process)
        self.assertIsNone(instance.error)
        self.assertIn(("close", 12), native.calls)

    def test_parent_cancel_stops_guard_but_preserves_initial_cancel_error(self):
        instance, native, process = self.make_parent_lease()
        checks = 0
        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise RuntimeError("cancel requested")
        def wait(timeout):
            lease.save_json(instance.report_path, dict(token=instance.token, phase="finished", restored=True))
        process.wait.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, "cancel requested"):
            instance.start_cancellable(check)
        self.assertIn(("set", 12), native.calls)
        process.kill.assert_not_called()
        self.assertIsNone(instance.process)


class LogonRecoveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "测试记录" / ("跨版本恢复入口-" + uuid.uuid4().hex)
        self.paths = RuntimePaths(base / "new build", base / "state").prepare()
        self.old_executable = base / "old build" / "八爪鱼震动桥.exe"
        self.old_executable.parent.mkdir()
        self.old_executable.write_bytes(b"")  # A marker only; never run this file.
        self.old_command = subprocess.list2cmdline([str(self.old_executable), "--data-dir", str(self.paths.state), "--recover-hot-switch"])
        self.record = dict(format=1, token=TOKEN, parent_pid=123, original_active=True,
                           restart_needed=False, restore_pending=False, device_id=IDS[0],
                           targets=TARGETS.to_dict(), journal=lease.legacy.JOURNAL, added_devices=[])
        self.value = self.old_command
        self.kind = 1
        self.registry = MagicMock(HKEY_CURRENT_USER=1, KEY_READ=2, KEY_SET_VALUE=4, REG_SZ=1)
        def query(*args):
            if self.value is None:
                raise FileNotFoundError()
            return self.value, self.kind
        def write(key, name, reserved, kind, value):
            # Registration must have durable ownership before a registry write.
            saved = json.loads((self.paths.state / lease.TRANSACTION).read_text(encoding="utf-8"))
            self.assertEqual(saved["recovery_command"], value)
            self.value, self.kind = value, kind
        def delete(*args):
            self.value = None
        self.registry.QueryValueEx.side_effect = query
        self.registry.SetValueEx.side_effect = write
        self.registry.DeleteValue.side_effect = delete
        patch.dict(lease.sys.modules, {"winreg": self.registry}).start()
        patch.object(lease.sys, "frozen", True, create=True).start()
        self.addCleanup(patch.stopall)

    def save(self, **changes):
        self.record.update(changes)
        lease.save_json(self.paths.state / lease.TRANSACTION, self.record)

    def test_old_record_without_command_removes_only_same_state_bridge_entry(self):
        self.save()
        lease.recovery_at_logon(self.paths, False)
        self.assertIsNone(self.value)
        self.registry.DeleteValue.assert_called_once()

    def test_recorded_old_build_command_is_removed_by_new_build(self):
        self.save(recovery_command=self.old_command)
        self.assertNotEqual(self.old_command, lease.recovery_command(self.paths))
        lease.recovery_at_logon(self.paths, False)
        self.assertIsNone(self.value)

    def test_recorded_command_survives_old_executable_missing(self):
        command = subprocess.list2cmdline([str(self.old_executable.parent / "missing" / "八爪鱼震动桥.exe"),
                                          "--data-dir", str(self.paths.state), "--recover-hot-switch"])
        self.value = command
        self.save(recovery_command=command)
        lease.recovery_at_logon(self.paths, False)
        self.assertIsNone(self.value)

    def test_missing_legacy_executable_is_not_accepted_by_fallback(self):
        self.value = subprocess.list2cmdline([str(self.old_executable.parent / "missing" / "八爪鱼震动桥.exe"),
                                              "--data-dir", str(self.paths.state), "--recover-hot-switch"])
        self.save()
        lease.recovery_at_logon(self.paths, False)
        self.registry.DeleteValue.assert_not_called()

    def test_changed_entry_not_equal_to_recorded_command_is_preserved(self):
        self.save(recovery_command=lease.recovery_command(self.paths))
        lease.recovery_at_logon(self.paths, False)
        self.assertEqual(self.value, self.old_command)
        self.registry.DeleteValue.assert_not_called()

    def test_other_data_directory_and_extra_switches_are_never_removed(self):
        cases = [
            [str(self.old_executable), "--data-dir", str(self.paths.state / "other"), "--recover-hot-switch"],
            [str(self.old_executable), "--data-dir", str(self.paths.state), "--recover-receiver-input"],
            [str(self.old_executable), "--data-dir", str(self.paths.state), "--recover-hot-switch", "--extra"],
            [str(self.old_executable), "--data-dir", str(self.paths.state), "--recover-hot-switch=1"],
            [str(self.old_executable), "--data-dir", ".", "--recover-hot-switch"],
            [str(self.old_executable.parent / "foreign.exe"), "--data-dir", str(self.paths.state), "--recover-hot-switch"],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.save()
                self.value = subprocess.list2cmdline(args)
                lease.recovery_at_logon(self.paths, False)
                self.registry.DeleteValue.assert_not_called()

    def test_even_recorded_command_cannot_own_other_data_directory(self):
        self.value = subprocess.list2cmdline([str(self.old_executable), "--data-dir", str(self.paths.state / "other"), "--recover-hot-switch"])
        self.save(recovery_command=self.value)
        lease.recovery_at_logon(self.paths, False)
        self.registry.DeleteValue.assert_not_called()

    def test_registration_cannot_overwrite_other_directory_entry(self):
        self.save(recovery_command=lease.recovery_command(self.paths))
        self.value = subprocess.list2cmdline([str(self.old_executable), "--data-dir", str(self.paths.state / "other"), "--recover-hot-switch"])
        with self.assertRaisesRegex(RuntimeError, "其他恢复会话"):
            lease.recovery_at_logon(self.paths, True)
        self.registry.SetValueEx.assert_not_called()
        self.registry.DeleteValue.assert_not_called()

    def test_pending_legacy_entry_is_adopted_without_replacing_old_command(self):
        self.save(restore_pending=True)
        lease.recovery_at_logon(self.paths, True)
        self.registry.SetValueEx.assert_not_called()
        saved = json.loads((self.paths.state / lease.TRANSACTION).read_text(encoding="utf-8"))
        self.assertEqual(saved["recovery_command"], self.old_command)
        self.assertEqual(self.value, self.old_command)

    def test_new_registration_records_exact_command_before_registry_write(self):
        self.value = None
        self.save(restore_pending=True)
        lease.recovery_at_logon(self.paths, True)
        self.assertEqual(self.value, lease.recovery_command(self.paths))
        self.registry.SetValueEx.assert_called_once()

    def test_windows_parser_handles_quoted_unicode_paths_without_execution(self):
        self.assertEqual(lease._command_argv(self.old_command),
                         [str(self.old_executable), "--data-dir", str(self.paths.state), "--recover-hot-switch"])
        self.assertEqual(lease._command_argv(self.old_command + " --extra"), [])
        self.assertEqual(lease._command_argv(self.old_command + "\0ignored"), [])


if __name__ == "__main__":
    unittest.main()
