import unittest
from unittest.mock import patch
from dsbridge.application.cli import run


class CliWorkersTests(unittest.TestCase):
    def test_workers_run_before_any_context_or_runtime_state(self):
        for flag, name in (("--hot-switch-worker", "hot_switch_service"), ("--controller-usage-worker", "controller_usage")):
            with self.subTest(flag=flag), patch("dsbridge.application.cli.ApplicationContext") as context, \
                    patch("dsbridge.application.cli.configure") as configure, \
                    patch("dsbridge.application.cli.RuntimeDiagnostics") as diagnostics, \
                    patch("dsbridge.platform.windows." + name + ".worker_main", return_value=0) as worker:
                self.assertEqual(run([flag, "encoded"]), 0)
                worker.assert_called_once_with("encoded")
                context.assert_not_called()
                configure.assert_not_called()
                diagnostics.assert_not_called()

    def test_worker_rejects_mixed_or_missing_arguments(self):
        for args in (["--hot-switch-worker"], ["--hot-switch-worker", "one", "--check-hot-switch"],
                     ["--controller-usage-worker", "one", "--hot-switch-worker", "two"]):
            with self.subTest(args=args), patch("dsbridge.application.cli.ApplicationContext") as context:
                self.assertEqual(run(args), 2)
                context.assert_not_called()
