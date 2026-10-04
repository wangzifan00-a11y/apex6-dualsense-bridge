import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from dependency_setup import (DependencyManager, PACKAGES, SETUP_STATE, WindowsProbe,
                              installer_arguments, run_installer, verify_package)


class FakeProbe:
    def __init__(self, installed=()):
        self.installed = set(installed)
        self.cli = set(installed)
        self.products = set()
        self.conflict = False
        self.denied = False
        self.is_supported = True
        self.version = "0.9.7.7"
        self.port_code = 0
        self.hide_error = False
        self.machine_boot = {"machine": "test-pc", "boot": "1"}

    def supported(self):
        return self.is_supported

    def identity(self):
        return dict(self.machine_boot)

    def service(self, name):
        if self.denied:
            raise PermissionError("registry access denied")
        return self.conflict if name == "HidGuardian" else ("usbip" if name == "usbip2_ude" else "hidhide") in self.installed

    def cli_exists(self, key):
        return key in self.cli

    def product_exists(self, key):
        return key in self.products

    def usbip_command(self, argument):
        return (0, self.version) if argument == "--version" else (self.port_code, "")

    def hidhide_ready(self):
        if self.hide_error:
            raise OSError("cannot open filter")


class DependencySetupTests(unittest.TestCase):
    def setUp(self):
        # Keep artifacts instead of bulk-removing temporary test folders.
        self.base = Path(__file__).resolve().parents[1] / "测试记录" / "依赖检查" / uuid.uuid4().hex
        self.base.mkdir(parents=True)
        self.probe = FakeProbe()
        self.calls = []
        self.verify_calls = []
        self.exit_code = 0
        self.cancel_uac = False
        self.provision = True
        self.bad_package = False

        def verify(base, package):
            self.verify_calls.append(package.key)
            return not self.bad_package, "test bundle"

        def launch(path, arguments):
            self.calls.append((path, arguments))
            if self.cancel_uac:
                exc = OSError("cancelled")
                exc.winerror = 1223
                raise exc
            key = "usbip" if path.name.startswith("USBip") else "hidhide"
            if self.exit_code in (0, 3010) and self.provision:
                self.probe.installed.add(key)
                self.probe.cli.add(key)
            return self.exit_code
        self.manager = DependencyManager(self.base, probe=self.probe, launcher=launch, verifier=verify, packages=PACKAGES[:2])

    def states(self):
        return [row.state for row in self.manager.check()]

    def test_fresh_machine_detected_without_controller(self):
        rows = self.manager.check()
        self.assertEqual([r.state for r in rows], ["missing", "missing"])
        self.assertTrue(all(r.can_install for r in rows))
        self.assertEqual(self.calls, [])

    def test_cli_alone_is_not_ready_or_safe_to_reinstall(self):
        self.probe.cli.update(("usbip", "hidhide"))
        self.assertEqual(self.states(), ["unavailable", "unavailable"])
        self.manager.install_missing()
        self.assertEqual(self.calls, [])

    def test_service_alone_does_not_authorize_reinstallation(self):
        self.probe.installed.update(("usbip", "hidhide"))
        self.assertEqual(self.states(), ["unavailable", "unavailable"])

    def test_custom_or_broken_product_install_detected(self):
        self.probe.products.update(("usbip", "hidhide"))
        self.assertEqual(self.states(), ["unavailable", "unavailable"])

    def test_access_denied_is_not_missing(self):
        self.probe.denied = True
        self.assertEqual(self.states(), ["error", "error"])
        self.manager.install_missing()
        self.assertEqual(self.calls, [])

    def test_wrong_usbip_version_not_replaced(self):
        self.probe.installed.update(("usbip", "hidhide"))
        self.probe.cli.update(self.probe.installed)
        self.probe.version = "0.9.8.0"
        self.assertEqual(self.states(), ["mismatch", "ready"])
        self.manager.install_missing()
        self.assertEqual(self.calls, [])

    def test_installed_but_unusable_driver_detected(self):
        self.probe.installed.update(("usbip", "hidhide"))
        self.probe.cli.update(self.probe.installed)
        self.probe.port_code = 1
        self.probe.hide_error = True
        self.assertEqual(self.states(), ["unavailable", "unavailable"])

    def test_hidguardian_conflict(self):
        self.probe.conflict = True
        self.assertEqual(self.states(), ["missing", "conflict"])
        self.manager.install_missing()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("USBip", self.calls[0][0].name)

    def test_unsupported_platform_not_installable(self):
        self.probe.is_supported = False
        self.assertEqual(self.states(), ["unsupported", "unsupported"])
        self.manager.install_missing()
        self.assertEqual(self.calls, [])

    def test_installs_both_in_order_and_verifies_result(self):
        report = self.manager.install_missing()
        self.assertEqual([r["outcome"] for r in report["results"]], ["installed", "installed"])
        self.assertEqual([r["state"] for r in report["dependencies"]], ["ready", "ready"])
        self.assertEqual([p.name for p, _ in self.calls], [p.filename for p in self.manager.packages])
        self.assertIn("/NORESTART", self.calls[0][1])
        self.assertIn("REBOOT=ReallySuppress", self.calls[1][1])
        self.assertGreaterEqual(self.verify_calls.count("usbip"), 2)
        self.assertGreaterEqual(self.verify_calls.count("hidhide"), 2)
        # Clicking again is a no-op, even if the UI showed stale missing state.
        self.manager.install_missing()
        self.assertEqual(len(self.calls), 2)

    def test_only_missing_dependency_installed(self):
        self.probe.installed.add("usbip")
        self.probe.cli.add("usbip")
        self.manager.install_missing()
        self.assertEqual([p.name for p, _ in self.calls], [PACKAGES[1].filename])

    def test_package_integrity_blocks_execution(self):
        self.bad_package = True
        self.manager.install_missing()
        self.assertEqual(self.calls, [])
        folder = self.base / "installers"
        folder.mkdir()
        self.assertFalse(verify_package(self.base, PACKAGES[0])[0])
        (folder / PACKAGES[0].filename).write_bytes(b"not the official installer")
        self.assertFalse(verify_package(self.base, PACKAGES[0])[0])

    def test_uac_cancel_stops_remaining_installs_and_can_retry(self):
        self.cancel_uac = True
        report = self.manager.install_missing()
        self.assertEqual(report["results"][0]["outcome"], "cancelled")
        self.assertEqual(len(self.calls), 1)
        self.cancel_uac = False
        self.manager.install_missing()
        self.assertEqual(self.states(), ["ready", "ready"])

    def test_failed_installer_is_not_success(self):
        self.exit_code = 1603
        report = self.manager.install_missing()
        self.assertEqual(report["results"][0]["outcome"], "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.states(), ["missing", "missing"])

    def test_installer_cancel_return_code(self):
        self.exit_code = 1602
        report = self.manager.install_missing()
        self.assertEqual(report["results"][0]["outcome"], "cancelled")
        self.assertEqual(len(self.calls), 1)

    def test_zero_exit_without_ready_driver_is_not_success(self):
        self.provision = False
        report = self.manager.install_missing()
        self.assertEqual([r["outcome"] for r in report["results"]], ["unavailable", "unavailable"])
        self.assertEqual(self.states(), ["missing", "missing"])

    def test_restart_required_survives_reopen_then_clears_after_boot(self):
        self.exit_code = 3010
        report = self.manager.install_missing()
        self.assertEqual([r["outcome"] for r in report["results"]], ["restart", "restart"])
        reopened = DependencyManager(self.base, probe=self.probe, verifier=lambda *_: (True, ""), packages=PACKAGES[:2])
        self.assertEqual([r.state for r in reopened.check()], ["restart", "restart"])
        self.probe.machine_boot["boot"] = "2"
        self.assertEqual(self.states(), ["ready", "ready"])

    def test_restart_record_not_transferred_to_other_pc(self):
        self.exit_code = 3010
        self.manager.install_missing()
        self.probe.machine_boot["machine"] = "other-pc"
        self.probe.installed.clear()
        self.probe.cli.clear()
        self.assertEqual(self.states(), ["missing", "missing"])

    def test_broken_restart_journal_cannot_authorize_installation(self):
        (self.base / SETUP_STATE).write_text("{bad json", encoding="utf-8")
        self.assertEqual(self.states(), ["error", "error"])

    def test_ps5_requires_dependencies_ps4_is_rejected(self):
        for mode in (1, 5):
            with self.assertRaisesRegex(RuntimeError, "依赖"):
                self.manager.require(mode)
        with self.assertRaisesRegex(ValueError, "仅支持 PS5"):
            self.manager.require(2)

    def test_concurrent_install_cannot_launch_second_installer(self):
        self.manager._install_lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "安装已在进行"):
                self.manager.install_missing()
        finally:
            self.manager._install_lock.release()
        self.assertEqual(self.calls, [])

    def test_windows_probe_distinguishes_missing_from_access_denied(self):
        probe = WindowsProbe()
        def missing(*args):
            raise FileNotFoundError()
        probe.registry_value = missing
        self.assertFalse(probe.service("HidHide"))
        def denied(*args):
            raise PermissionError()
        probe.registry_value = denied
        with self.assertRaises(PermissionError):
            probe.service("HidHide")

    def test_native_unicode_process_handle_and_exit_code(self):
        # Exercise the same native launch/wait/exit path without elevation,
        # installing a driver, writing outside the workspace, or any deletion.
        script = self.base / "中文 路径子进程.py"
        output = self.base / "子进程完成.txt"
        script.write_text("from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ok',encoding='utf-8')\nsys.exit(23)\n", encoding="utf-8")
        code = run_installer(Path(sys.executable), [str(script), str(output)], verb="open")
        self.assertEqual(code, 23)
        self.assertEqual(output.read_text(encoding="utf-8"), "ok")


if __name__ == "__main__":
    unittest.main()
