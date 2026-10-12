"""First-use setup gates with fake service/UAC; never install or restart anything."""
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

from dsbridge.platform.windows import hot_switch_setup as setup
from dsbridge.platform.windows.hot_switch_service import API_VERSION, HELPER_VERSION, HotSwitchError
from dsbridge.runtime.paths import RuntimePaths

SID = "S-1-5-21-100-200-300-1001"


def ready(**changes):
    return {"ok": True, "api_version": API_VERSION, "version": HELPER_VERSION,
            "permitted_owner": SID, "restart_required": False, **changes}


def missing():
    return HotSwitchError("service_missing", "missing")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.paths = RuntimePaths(Path.cwd(), Path.cwd())
        self.client = Mock()
        self.artifact = self.paths.assets / "native/hotswitch/bin/DSBridgeHotSwitch.exe"
        self.notify, self.cancel = Mock(), Mock()
        self.patches = [patch.object(setup, "HotSwitchClient", return_value=self.client),
                        patch.object(setup, "current_user_sid", return_value=SID),
                        patch.object(setup, "verify_helper_artifact", return_value=self.artifact),
                        patch.object(setup, "run_installer", return_value=0),
                        patch.object(setup, "_gate", threading.Lock()),
                        patch.object(setup, "_attempted", set())]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.verify, self.install = self.mocks[2:4]

    def run_setup(self):
        return setup.ensure_hot_switch_service(self.paths, self.notify, self.cancel)

    def test_existing_valid_service_does_not_validate_or_install_artifact(self):
        self.client.status.return_value = ready()
        self.assertEqual(self.run_setup(), ready())
        self.verify.assert_not_called()
        self.install.assert_not_called()
        self.notify.assert_not_called()

    def test_pending_recovery_is_readable_but_not_ready_or_reinstalled(self):
        self.client.status.return_value = ready(recovery_pending=True)
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "recovery_pending")
        self.install.assert_not_called()
        self.verify.assert_not_called()
        self.assertFalse(setup._attempted)

    def test_pending_system_restart_has_explicit_chinese_guidance(self):
        self.client.status.return_value = ready(recovery_pending=True, recovery_restart_required=True)
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "restart_required")
        self.assertIn("重启电脑", str(raised.exception))
        self.assertTrue(raised.exception.details["recovery_restart_required"])
        self.install.assert_not_called()

    def test_explicit_upgrade_validates_fixed_artifact_and_rechecks_status(self):
        self.client.status.side_effect = [HotSwitchError("version_mismatch", "old"), ready()]
        self.assertEqual(setup.install_hot_switch_service(self.paths, self.notify, self.cancel), ready())
        self.verify.assert_called_once_with(self.paths)
        self.install.assert_called_once_with(self.artifact, ["--install", "--owner-sid", SID])

    def test_explicit_repair_does_not_repeat_after_cancellation(self):
        self.client.status.side_effect = HotSwitchError("pipe_read_failed", "broken")
        error = OSError("cancelled")
        error.winerror = 1223
        self.install.side_effect = error
        with self.assertRaises(HotSwitchError) as raised:
            setup.install_hot_switch_service(self.paths, self.notify, self.cancel)
        self.assertEqual(raised.exception.code, "setup_cancelled")
        with self.assertRaises(HotSwitchError) as raised:
            setup.install_hot_switch_service(self.paths, self.notify, self.cancel)
        self.assertEqual(raised.exception.code, "setup_already_attempted")
        self.assertEqual(self.install.call_count, 1)

    def test_explicit_install_running_but_pending_is_not_bridge_ready(self):
        self.client.status.side_effect = [missing(), ready(recovery_pending=True)]
        with self.assertRaises(HotSwitchError) as raised:
            setup.install_hot_switch_service(self.paths, self.notify, self.cancel)
        self.assertEqual(raised.exception.code, "recovery_pending")
        self.assertEqual(self.install.call_count, 1)

    def test_explicit_install_does_not_restart_a_valid_pending_service(self):
        self.client.status.return_value = ready(recovery_pending=True)
        with self.assertRaises(HotSwitchError) as raised:
            setup.install_hot_switch_service(self.paths, self.notify, self.cancel)
        self.assertEqual(raised.exception.code, "recovery_pending")
        self.install.assert_not_called()

    def test_explicit_upgrade_rejects_changed_artifact_before_uac(self):
        self.client.status.side_effect = HotSwitchError("version_mismatch", "old")
        self.verify.side_effect = HotSwitchError("helper_unverified", "changed")
        with self.assertRaises(HotSwitchError) as raised:
            setup.install_hot_switch_service(self.paths, self.notify, self.cancel)
        self.assertEqual(raised.exception.code, "helper_unverified")
        self.install.assert_not_called()

    def test_only_verified_missing_service_can_trigger_installer(self):
        for code in ("service_access_denied", "service_not_running", "owner_mismatch", "version_mismatch", "pipe_unavailable"):
            self.client.status.side_effect = HotSwitchError(code, "no")
            with self.subTest(code=code), self.assertRaises(HotSwitchError) as raised:
                self.run_setup()
            self.assertEqual(raised.exception.code, code)
        self.verify.assert_not_called()
        self.install.assert_not_called()

    def test_first_install_uses_only_fixed_path_and_owner_arguments_then_rechecks(self):
        self.client.status.side_effect = [missing(), missing(), ready()]
        self.assertEqual(self.run_setup(), ready())
        self.verify.assert_called_once_with(self.paths)
        self.install.assert_called_once_with(self.artifact, ["--install", "--owner-sid", SID])
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.client.status.call_count, 3)
        self.assertEqual(self.mocks[1].call_count, 2)

    def test_another_completed_installer_is_detected_before_new_prompt(self):
        self.client.status.side_effect = [missing(), ready()]
        self.assertEqual(self.run_setup(), ready())
        self.install.assert_not_called()
        self.notify.assert_not_called()

    def test_bad_hash_never_reaches_elevation(self):
        self.client.status.side_effect = missing()
        self.verify.side_effect = HotSwitchError("helper_unverified", "changed")
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "helper_unverified")
        self.install.assert_not_called()
        self.notify.assert_not_called()

    def test_uac_cancel_is_one_attempt_and_does_not_auto_relaunch(self):
        self.client.status.side_effect = missing()
        error = OSError("cancelled")
        error.winerror = 1223
        self.install.side_effect = error
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "setup_cancelled")
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "setup_already_attempted")
        self.assertIn("重新打开程序", str(raised.exception))
        self.assertEqual(self.install.call_count, 1)
        self.assertEqual(self.notify.call_count, 1)

    def test_failed_exit_is_not_success_and_no_relaunch(self):
        self.client.status.side_effect = missing()
        self.install.return_value = 1
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "setup_failed")
        with self.assertRaises(HotSwitchError):
            self.run_setup()
        self.assertEqual(self.install.call_count, 1)

    def test_already_attempted_does_not_hide_later_valid_service(self):
        setup._attempted.add((str(self.paths.assets).casefold(), SID))
        self.client.status.return_value = ready()
        self.assertEqual(self.run_setup(), ready())
        self.install.assert_not_called()

    def test_success_exit_alone_is_insufficient(self):
        self.client.status.side_effect = [missing(), missing(), missing()]
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "service_missing")
        self.assertEqual(self.install.call_count, 1)

    def test_final_status_is_bound_to_pre_elevation_user(self):
        self.client.status.side_effect = [missing(), missing(), ready(permitted_owner="S-1-5-18")]
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "owner_mismatch")

    def test_changed_caller_identity_stops_before_status_acceptance(self):
        self.client.status.side_effect = missing()
        self.mocks[1].side_effect = [SID, "S-1-5-21-100-200-300-1002"]
        with self.assertRaises(HotSwitchError) as raised:
            self.run_setup()
        self.assertEqual(raised.exception.code, "owner_mismatch")
        self.assertEqual(self.client.status.call_count, 2)

    def test_cancel_before_elevation_does_not_install(self):
        self.client.status.side_effect = missing()
        self.cancel.side_effect = [None, None, RuntimeError("stop requested")]
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            self.run_setup()
        self.install.assert_not_called()
        self.assertFalse(setup._attempted)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        # Preserve explicit test artifacts; do not recursively remove directories.
        self.root = Path(__file__).resolve().parents[1] / "测试记录/热切换安装" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.paths = RuntimePaths(self.root, self.root)

    def fixture(self, development=False):
        relative = "native/hotswitch/bin/DSBridgeHotSwitch.exe" if development else "native/hotswitch/DSBridgeHotSwitch.exe"
        artifact = self.root / relative
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"MZ" + b"\x00" * 2046)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if development:
            source = self.root / "native/hotswitch/Program.cs"
            source.write_text("// pure fixture", encoding="utf-8")
            manifest = {"api_version": API_VERSION, "version": HELPER_VERSION, "sha256": digest,
                        "sources": {"native/hotswitch/Program.cs": hashlib.sha256(source.read_bytes()).hexdigest()}}
            record = artifact.parent / "build-manifest.json"
        else:
            manifest = {"files": {relative: digest}}
            record = self.root / "release-manifest.json"
        record.write_text(json.dumps(manifest), encoding="utf-8")
        return artifact, record, manifest

    def verify(self, artifact):
        with patch.object(setup, "helper_artifact", return_value=artifact):
            return setup.verify_helper_artifact(self.paths)

    def test_release_manifest_covers_exact_binary(self):
        artifact, _, _ = self.fixture()
        self.assertEqual(self.verify(artifact), artifact)
        artifact.write_bytes(b"MZ" + b"changed" * 512)
        with self.assertRaises(HotSwitchError) as raised:
            self.verify(artifact)
        self.assertEqual(raised.exception.code, "helper_unverified")

    def test_missing_entry_or_invalid_digest_is_rejected(self):
        artifact, record, manifest = self.fixture()
        for contents in ({"files": {}}, {"files": {"native/hotswitch/DSBridgeHotSwitch.exe": "no"}}, []):
            record.write_text(json.dumps(contents), encoding="utf-8")
            with self.subTest(contents=contents), self.assertRaises(HotSwitchError):
                self.verify(artifact)

    def test_development_build_requires_current_source_and_exact_version(self):
        artifact, record, manifest = self.fixture(development=True)
        self.assertEqual(self.verify(artifact), artifact)
        manifest["api_version"] = True
        record.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(HotSwitchError):
            self.verify(artifact)
        manifest["api_version"] = API_VERSION
        record.write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "native/hotswitch/Program.cs").write_text("// changed", encoding="utf-8")
        with self.assertRaises(HotSwitchError):
            self.verify(artifact)

    def test_arbitrary_path_is_rejected_before_open(self):
        with self.assertRaises(HotSwitchError) as raised:
            self.verify(self.root / "other.exe")
        self.assertEqual(raised.exception.code, "helper_unverified")

    def test_reparse_file_rejected_before_hash(self):
        artifact, _, _ = self.fixture()
        original = Path.lstat
        def details(path):
            if path == artifact:
                return type("Attributes", (), {"st_mode": 0o100644, "st_file_attributes": 0x400})()
            return original(path)
        with patch.object(Path, "lstat", details), self.assertRaises(HotSwitchError) as raised:
            self.verify(artifact)
        self.assertEqual(raised.exception.code, "helper_unverified")


if __name__ == "__main__":
    unittest.main()
