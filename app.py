"""Compatibility entry point for the modular DS bridge."""
import sys
from pathlib import Path
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / ".localdeps"))

from dsbridge.core.engine import Engine, scale_feedback
from dsbridge.core.session_log import FeedbackSessionLog
from dsbridge.core.modes import MODES, MODE_IDS, DETAILS
from dsbridge.ui.window import run_gui

if __name__ == "__main__":
    from dsbridge.application.cli import run
    raise SystemExit(run())
