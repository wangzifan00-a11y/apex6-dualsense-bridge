"""Fresh-PC and relocatable resource checks; installers and drivers are faked."""
from dataclasses import replace
from pathlib import Path
import json
import unittest
from unittest.mock import Mock

from dsbridge.dependencies.catalog import Package, PACKAGES
from dsbridge.dependencies.manager import DependencyManager
from dsbridge.runtime.paths import RuntimePaths
from test_dependency_setup import FakeProbe
from test_modular_architecture import folder, ROOT


class PortableDependencyTests(unittest.TestCase):
    def setUp(self):
        base = folder()
        self.paths = RuntimePaths(base / "只读资源", base / "数据").prepare()
        self.paths.assets.mkdir()
        self.probe = FakeProbe()
        self.calls = []
        def launch(path, arguments):
            self.calls.append((path, arguments))
            key = next(p.key for p in PACKAGES if p.filename == path.name)
            self.probe.installed.add(key)
            self.probe.cli.add(key)
            return 0
        self.manager = DependencyManager(self.paths, probe=self.probe, launcher=launch,
                                         verifier=lambda *_: (True, "verified"))

    def test_only_ps5_dependencies_installed_from_assets_and_logged_to_state(self):
        self.assertTrue(all(row.state == "missing" for row in self.manager.check()))
        self.assertEqual(self.calls, [])
        result = self.manager.install_missing()
        self.assertEqual([r["outcome"] for r in result["results"]], ["installed"] * 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual({p.key for p in PACKAGES}, {"usbip", "hidhide"})
        self.assertTrue(all(p.parent == self.paths.assets / "installers" for p, _ in self.calls))
        self.assertTrue((self.paths.state / "dependency-install-result.json").is_file())
        self.assertFalse((self.paths.assets / "logs").exists())
        self.assertEqual(list(self.paths.assets.iterdir()), [])

    def test_old_ps4_install_cannot_enable_removed_mode(self):
        self.probe.installed.update(("hidhide", "vigem"))
        self.probe.cli.add("hidhide")
        with self.assertRaisesRegex(ValueError, "仅支持 PS5"):
            self.manager.require(2)
        with self.assertRaisesRegex(RuntimeError, "USBip"):
            self.manager.require(1)

    def test_ps5_does_not_require_vigem(self):
        self.probe.installed.update(("hidhide", "usbip"))
        self.probe.cli.update(self.probe.installed)
        self.manager.require(1)
        self.manager.require(5)
        with self.assertRaisesRegex(ValueError, "仅支持 PS5"):
            self.manager.require(2)

    def test_vigem_is_never_queried_or_installed(self):
        self.probe.installed.update(("hidhide", "usbip", "vigem"))
        self.probe.cli.update(self.probe.installed)
        original_service = self.probe.service
        def service(name):
            self.assertNotEqual(name, "ViGEmBus")
            return original_service(name)
        self.probe.service = service
        self.assertEqual([row.state for row in self.manager.check()], ["ready", "ready"])
        self.manager.install_missing()
        self.assertEqual(self.calls, [])

    def test_custom_retired_driver_package_cannot_be_installed(self):
        old = Package("vigem", "ViGEmBus", "old", "old.exe", "test", "official")
        manager = DependencyManager(self.paths, probe=self.probe, launcher=Mock(),
                                    verifier=lambda *_: (True, ""), packages=(old,))
        self.assertEqual(manager.check()[0].state, "unsupported")
        manager.install_missing()
        manager.launcher.assert_not_called()

    def test_virtual_sony_identity_is_generated_in_state_not_resources(self):
        from dsbridge.virtual.dualsense.runtime import LocalViiper
        backend = LocalViiper(self.paths, {})
        identity = self.paths.state / "controller-identity.json"
        self.assertTrue(identity.is_file())
        self.assertFalse((self.paths.assets / identity.name).exists())
        old = identity.read_text(encoding="utf-8")
        LocalViiper(self.paths, {})
        self.assertEqual(identity.read_text(encoding="utf-8"), old)
        self.assertEqual(backend.paths.assets, self.paths.assets)

    def test_recovery_command_uses_program_and_state_separately(self):
        from dsbridge.controllers.flydigi.apex6.recovery import recovery_command
        command = recovery_command(self.paths)
        self.assertIn(str(self.paths.assets / "八爪鱼震动桥.exe"), command)
        self.assertIn("--data-dir", command)
        self.assertIn(str(self.paths.state), command)

    def test_release_rejects_machine_state_and_shortcuts_without_deleting_them(self):
        from tools.release_build import verify_clean
        path = folder()
        verify_clean(path)
        leaked = path / "audio-defaults.json"
        leaked.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "本机"):
            verify_clean(path)
        self.assertTrue(leaked.exists())
