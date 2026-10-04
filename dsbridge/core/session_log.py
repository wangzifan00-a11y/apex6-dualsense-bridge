"""Privacy-preserving feedback session summaries."""
import json
from pathlib import Path
import time

class FeedbackSessionLog:
    """Aggregate diagnostics written on the UI thread, never the motor loop."""
    def __init__(self, path):
        self.path = Path(path)
        self.last_write = 0.0
        self.session_id = None

    def write(self, record, *, final=False):
        now = time.monotonic()
        if not final and record["session_id"] == self.session_id and now - self.last_write < 1:
            return
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.last_write, self.session_id = now, record["session_id"]
