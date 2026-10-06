import gc
import json
from pathlib import Path
import time
import tkinter as tk
import unittest
import uuid

from dependency_setup import DependencyManager, PACKAGES
from dependency_ui import DependencyPanel
from test_dependency_setup import FakeProbe


class DependencyUITests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[1] / "测试记录" / "依赖界面" / uuid.uuid4().hex
        self.base.mkdir(parents=True)
        self.probe = FakeProbe()
        self.calls = []
        self.code = 0
        self.cancel = False
        self.slow = False
        def launcher(path, args):
            self.calls.append(path.name)
            if self.slow:
                time.sleep(.2)
            if self.cancel:
                exc = OSError("UAC cancelled")
                exc.winerror = 1223
                raise exc
            key = "usbip" if path.name.startswith("USBip") else "hidhide"
            if self.code in (0, 3010):
                self.probe.installed.add(key)
                self.probe.cli.add(key)
            return self.code
        self.manager = DependencyManager(self.base, probe=self.probe, launcher=launcher, verifier=lambda *_: (True, "离线包已校验"), packages=PACKAGES[:2])
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = DependencyPanel(self.root, self.base, manager=self.manager)
        self.until(lambda: self.panel.rows is not None and not self.panel.checking)
        self.panel.open()
        self.panel.dialog.withdraw()

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("GUI operation timed out")
            self.root.update()
            time.sleep(.01)
        self.root.update_idletasks()

    def tearDown(self):
        self.until(lambda: not self.panel.installing)
        self.root.destroy()
        self.panel = self.root = None
        # Do not leave destroyed Tcl variables for a later bridge worker's GC.
        gc.collect()

    def test_missing_opens_setup_before_controller_and_allows_install(self):
        self.assertFalse(self.panel.guard_start(5))
        self.assertIn("USBip 未安装", self.panel.summary.get())
        self.assertEqual(str(self.panel.install_button["state"]), "normal")
        self.assertFalse(self.panel.guard_start(2))
        self.assertFalse(self.panel.guard_start(0))

    def test_install_button_runs_both_then_becomes_disabled(self):
        self.panel.install_button.invoke()
        self.until(lambda: not self.panel.installing)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(str(self.panel.install_button["state"]), "disabled")
        self.assertTrue(self.panel.guard_start(5))
        self.assertIn("HidHide 已就绪", self.panel.summary.get())

    def test_pending_restart_blocks_start_and_reinstall(self):
        self.code = 3010
        self.panel.install_button.invoke()
        self.until(lambda: not self.panel.installing)
        self.assertFalse(self.panel.guard_start(5))
        self.assertIn("需要重启", self.panel.summary.get())
        self.assertEqual(str(self.panel.install_button["state"]), "disabled")

    def test_uac_cancel_keeps_ui_responsive_and_retry_available(self):
        self.cancel = True
        self.panel.install_button.invoke()
        self.until(lambda: not self.panel.installing)
        self.assertIn("已取消", self.panel.last_message)
        self.assertEqual(str(self.panel.install_button["state"]), "normal")
        self.assertEqual(len(self.calls), 1)

    def test_installation_blocks_start_and_close_until_done(self):
        self.slow = True
        self.panel.install_button.invoke()
        self.assertFalse(self.panel.guard_start(2))
        self.assertEqual(str(self.panel.close_button["state"]), "disabled")
        self.panel.close()
        self.assertIsNotNone(self.panel.dialog)
        self.until(lambda: not self.panel.installing)
        self.panel.close()
        self.assertIsNone(self.panel.dialog)

    def test_conversion_disables_installation(self):
        self.panel.set_conversion_busy(True)
        self.panel.install()
        self.assertEqual(self.calls, [])
        self.assertEqual(str(self.panel.install_button["state"]), "disabled")
        self.panel.set_conversion_busy(False)
        self.assertEqual(str(self.panel.install_button["state"]), "normal")

    def test_cancelled_install_dialog_fits_requested_layout(self):
        self.cancel = True
        self.panel.install_button.invoke()
        self.until(lambda: not self.panel.installing)
        # Requested size comes from actual Tk font metrics and wrapped labels.
        self.assertLessEqual(self.panel.dialog.winfo_reqheight(), 570)
        self.assertLessEqual(self.panel.dialog.winfo_reqwidth(), 740)


if __name__ == "__main__":
    unittest.main()
