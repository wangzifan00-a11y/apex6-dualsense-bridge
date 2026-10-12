"""Recovery cannot claim success just because its worker returned normally."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from dsbridge.application.cli import main, run
from dsbridge.application.context import ApplicationContext, require_input_recovered
from dsbridge.runtime.paths import RuntimePaths


class RecoveryResultTests(unittest.TestCase):
    def test_only_explicit_complete_recovery_is_success(self):
        result = {"restored": True, "refresh_pending": False, "warnings": []}
        self.assertIs(require_input_recovered(result), result)
        for bad in (None, {}, {"restored": True}, {"restored": True, "refresh_pending": True},
                    {"restored": False, "refresh_pending": True, "refresh_error": "needs restart"},
                    {"restored": True, "refresh_pending": False, "warnings": ["audio still pending"]}):
            with self.subTest(result=bad), self.assertRaises(RuntimeError):
                require_input_recovered(bad)

    def test_context_does_not_drop_pending_result_or_continue_legacy_cleanup(self):
        context = ApplicationContext.__new__(ApplicationContext)
        context.paths = object()
        with patch("dsbridge.platform.windows.hot_switch_lease.recover", return_value={
                    "restored": False, "refresh_pending": True, "refresh_error": "needs restart"}), \
                patch("dsbridge.controllers.flydigi.apex6.recovery.recover") as legacy:
            with self.assertRaisesRegex(RuntimeError, "needs restart"):
                context.recover()
        legacy.assert_not_called()

    def test_context_keeps_successful_recovery_result(self):
        context = ApplicationContext.__new__(ApplicationContext)
        context.paths = object()
        result = {"restored": True, "refresh_pending": False, "warnings": []}
        with patch("dsbridge.platform.windows.hot_switch_lease.recover", return_value=result), \
                patch("dsbridge.controllers.flydigi.apex6.recovery.recover") as legacy:
            self.assertIs(context.recover(), result)
        legacy.assert_called_once_with(context.paths)


class RecoveryCliTests(unittest.TestCase):
    def context(self):
        return SimpleNamespace(paths=RuntimePaths(Path.cwd(), Path.cwd()),
                               diagnostics=MagicMock(), recover=MagicMock())

    def test_explicit_pending_recovery_saves_report_and_returns_nonzero(self):
        context = self.context()
        pending = {"restored": False, "refresh_pending": True, "refresh_error": "needs restart"}
        with patch("dsbridge.application.cli.configure", return_value=context.paths), \
                patch("dsbridge.application.cli.ApplicationContext", return_value=context), \
                patch("dsbridge.application.cli.RuntimeDiagnostics") as diagnostics, \
                patch("dsbridge.platform.windows.hot_switch_lease.recover", return_value=pending), \
                patch("dsbridge.dependencies.manager.write_report") as write, \
                patch("dsbridge.application.cli.sys.stderr", None):
            self.assertEqual(run(["--recover-hot-switch"]), 1)
        write.assert_called_once_with(context.paths / "hot-switch-recovery-result.json", pending)
        diagnostics.return_value.exception.assert_called_once()

    def test_explicit_completed_recovery_returns_zero(self):
        context = self.context()
        with patch("dsbridge.application.cli.configure", return_value=context.paths), \
                patch("dsbridge.application.cli.ApplicationContext", return_value=context), \
                patch("dsbridge.application.cli.RuntimeDiagnostics"), \
                patch("dsbridge.platform.windows.hot_switch_lease.recover", return_value={
                    "restored": True, "refresh_pending": False}), \
                patch("dsbridge.dependencies.manager.write_report"):
            self.assertEqual(run(["--recover-hot-switch"]), 0)

    def test_gui_still_opens_with_visible_recovery_warning(self):
        context = self.context()
        context.recover.side_effect = RuntimeError("device enable still pending")
        with patch("dsbridge.ui.window.run_gui") as gui:
            main(["--ui-smoke"], context)
        self.assertEqual(gui.call_args.kwargs["startup_recovery_warning"], "device enable still pending")
        self.assertIs(gui.call_args.kwargs["context"], context)
        context.diagnostics.event.assert_called_once_with("input_recovery_pending", error="device enable still pending")

    def test_normal_gui_opens_without_recovery_warning(self):
        context = self.context()
        with patch("dsbridge.ui.window.run_gui") as gui:
            main(["--ui-smoke"], context)
        self.assertIsNone(gui.call_args.kwargs["startup_recovery_warning"])

    def test_explicit_install_uses_explicit_upgrade_api(self):
        context = self.context()
        with patch("dsbridge.platform.windows.hot_switch_setup.install_hot_switch_service", return_value={"ok": True}) as install, \
                patch("dsbridge.platform.windows.hot_switch_setup.ensure_hot_switch_service") as ensure, \
                patch("dsbridge.dependencies.manager.write_report"):
            main(["--install-hot-switch"], context)
        self.assertEqual(install.call_args.args[0], context.paths)
        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
