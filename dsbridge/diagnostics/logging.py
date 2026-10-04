"""Local lifecycle and exception diagnostics; no inputs or audio are recorded."""
from datetime import datetime, timezone
import faulthandler
import json
import os
from pathlib import Path
import threading
import traceback


class RuntimeDiagnostics:
    def __init__(self, base):
        self.path = Path(base) / "logs" / "app-events.jsonl"
        self.lock = threading.Lock()
        self.fault_file = None

    def event(self, name, **details):
        record = {"time": datetime.now(timezone.utc).isoformat(),
                  "pid": os.getpid(), "event": name, **details}
        try:
            self.path.parent.mkdir(exist_ok=True)
            with self.lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Diagnostics must not interrupt controller output or cleanup.

    def exception(self, context, exc_type, exc, tb):
        self.event("exception", context=context,
                   traceback="".join(traceback.format_exception(exc_type, exc, tb)))

    def start(self):
        self.event("process_start")
        try:
            self.fault_file = (self.path.parent / "native-faults.log").open("a", encoding="utf-8")
            self.fault_file.write(f"\nPID {os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
            self.fault_file.flush()
            faulthandler.enable(file=self.fault_file, all_threads=True)
        except (OSError, RuntimeError):
            if self.fault_file:
                self.fault_file.close()
                self.fault_file = None

    def close(self):
        self.event("process_exit")
        if self.fault_file:
            faulthandler.disable()
            self.fault_file.close()
            self.fault_file = None
