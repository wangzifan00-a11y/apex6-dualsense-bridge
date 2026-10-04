import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from receiver_input_guard import JOURNAL, hide_for_session, restore_record, save_json
from receiver_backend import ReceiverFeedback
from test_receiver_dsp import FakeSession, FakeViiper


TARGETS = [r"HID\VID_37D7&PID_2502&IG_00\8&abc&0&0000",
           r"USB\VID_37D7&PID_2502&IG_00\7&abc&0&00",
           r"USB\VID_37D7&PID_2502&MI_00\6&abc&0&0000"]
BLUETOOTH = r"BTHENUM\existing-bluetooth"


class FakeDriver:
    def __init__(self, *, hidden=None, active=True):
        self.config = dict(hidden_devices=list(hidden or []), active=active, inverse=False, allowed_nt=[])
        self.before_hide = lambda: None
        self.fail_after_hide = False
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        pass
    def snapshot(self):
        return copy.deepcopy(self.config)
    def hidden(self, values):
        self.before_hide()
        self.config["hidden_devices"] = list(values)
        if self.fail_after_hide:
            self.fail_after_hide = False
            raise OSError("lost completion after successful write")
    def active(self, active):
        self.config["active"] = active
    def allow(self, executable):
        self.config["allowed_nt"].append(str(executable))


class InputRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Preserve all test artifacts: the user's workspace forbids bulk deletion.
        self.base = Path(__file__).resolve().parents[1] / "测试记录" / ("输入恢复-" + uuid.uuid4().hex)
        self.base.mkdir(parents=True)
        self.registry = patch("receiver_input_guard.recovery_at_logon")
        self.run_entry = self.registry.start()
        self.addCleanup(self.registry.stop)

    def hide(self, driver):
        return hide_for_session(self.base, "a" * 32, 123, targets=TARGETS, control_factory=lambda: driver)

    def restore(self, driver):
        return restore_record(self.base, control_factory=lambda: driver)

    def test_stop_preserves_bluetooth_and_new_unrelated_hiding(self):
        driver = FakeDriver(hidden=[BLUETOOTH])
        self.hide(driver)
        driver.config["hidden_devices"] += ["UNRELATED_NEW_DEVICE", TARGETS[1].upper()]
        self.restore(driver)
        self.assertEqual(driver.config["hidden_devices"], [BLUETOOTH, "UNRELATED_NEW_DEVICE"])
        self.assertTrue(driver.config["active"])
        self.assertFalse(json.loads((self.base / JOURNAL).read_text())["restore_pending"])

    def test_restore_plan_exists_before_device_visibility_changes(self):
        driver = FakeDriver(active=False)
        def read_plan():
            plan = json.loads((self.base / JOURNAL).read_text())
            self.assertTrue(plan["restore_pending"])
            self.assertEqual(plan["added_devices"], TARGETS)
            self.run_entry.assert_called_with(self.base, True)
        driver.before_hide = read_plan
        self.hide(driver)
        driver.before_hide = lambda: None
        self.restore(driver)
        self.assertFalse(driver.config["active"])
        self.assertEqual(driver.config["hidden_devices"], [])
        self.run_entry.assert_called_with(self.base, False)

    def test_failed_start_after_hidden_write_is_recoverable(self):
        driver = FakeDriver()
        driver.fail_after_hide = True
        with self.assertRaises(OSError):
            self.hide(driver)
        self.assertEqual(driver.config["hidden_devices"], TARGETS)
        self.restore(driver)
        self.assertEqual(driver.config["hidden_devices"], [])

    def test_inverse_or_disabled_other_hiding_is_not_changed(self):
        for inverse in (True, False):
            driver = FakeDriver(hidden=[BLUETOOTH], active=False)
            driver.config["inverse"] = inverse
            before = driver.snapshot()
            with self.assertRaises(RuntimeError):
                self.hide(driver)
            self.assertEqual(driver.snapshot(), before)

    def test_recovery_rejects_non_receiver_target_and_preserves_pending_record(self):
        driver = FakeDriver(hidden=[BLUETOOTH])
        save_json(self.base / JOURNAL, dict(format=1, restore_pending=True, added_devices=[BLUETOOTH], original_active=True))
        with self.assertRaises(RuntimeError):
            self.restore(driver)
        self.assertEqual(driver.config["hidden_devices"], [BLUETOOTH])
        self.assertTrue(json.loads((self.base / JOURNAL).read_text())["restore_pending"])

    def test_restore_readback_failure_keeps_logon_recovery(self):
        driver = FakeDriver()
        self.hide(driver)
        driver.hidden = lambda values: None
        with self.assertRaises(RuntimeError):
            self.restore(driver)
        self.assertTrue(json.loads((self.base / JOURNAL).read_text())["restore_pending"])
        self.run_entry.assert_called_with(self.base, True)


class BackendLeaseTests(unittest.TestCase):
    def make_backend(self, fail_start=False, fail_stop=False):
        calls = []
        class Lease:
            error = None
            def __init__(self, base):
                pass
            def start(self):
                calls.append("hidden")
            def stop(self):
                calls.append("input restored")
        class Viiper(FakeViiper):
            def start(self):
                calls.append("virtual created")
                if fail_start:
                    raise RuntimeError("start failed")
            def stop(self):
                calls.append("virtual removed")
                if fail_stop:
                    raise RuntimeError("stop failed")
        instance = ReceiverFeedback(Path("."), {}, session_factory=FakeSession, viiper_factory=Viiper,
                                    monitor_factory=None, isolation_factory=Lease)
        return instance, calls

    def test_input_is_restored_after_virtual_device_removal(self):
        instance, calls = self.make_backend()
        instance.start()
        instance.stop()
        self.assertEqual(calls, ["hidden", "virtual created", "virtual removed", "input restored"])
        self.assertTrue(instance.session.restored)

    def test_failed_virtual_start_restores_input_and_motor_configuration(self):
        instance, calls = self.make_backend(fail_start=True)
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            instance.start()
        self.assertEqual(calls[-2:], ["virtual removed", "input restored"])
        self.assertTrue(instance.session.restored)

    def test_virtual_cleanup_exception_cannot_skip_input_restore(self):
        instance, calls = self.make_backend(fail_stop=True)
        instance.start()
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            instance.stop()
        self.assertEqual(calls[-1], "input restored")
        self.assertTrue(instance.session.restored)


if __name__ == "__main__":
    unittest.main()
