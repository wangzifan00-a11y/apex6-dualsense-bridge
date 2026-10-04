import json
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

from runtime_diagnostics import RuntimeDiagnostics


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_python_exception_and_exit_reason_are_persisted(self):
        base = Path(__file__).resolve().parents[1] / "测试记录" / ("程序异常-" + uuid.uuid4().hex)
        base.mkdir(parents=True, exist_ok=True)
        diagnostics = RuntimeDiagnostics(base)
        diagnostics.event("window_close_requested", engine_running=True)
        try:
            raise ValueError("specific callback failure")
        except ValueError:
            diagnostics.exception("tk_callback", *sys.exc_info())
        records = [json.loads(line) for line in diagnostics.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["event"], "window_close_requested")
        self.assertTrue(records[0]["engine_running"])
        self.assertEqual(records[1]["context"], "tk_callback")
        self.assertIn("ValueError: specific callback failure", records[1]["traceback"])

    def test_unwritable_diagnostic_log_does_not_interrupt_cleanup(self):
        diagnostics = RuntimeDiagnostics(Path(__file__).resolve().parents[1])
        with patch("runtime_diagnostics.Path.open", side_effect=PermissionError("read only")):
            diagnostics.event("window_loop_ended")
