"""Emit synthetic game feedback into our virtual Sony device, then verify output.

This is not a claim that any commercial game has been tested. Physical output
requires the explicit --physical flag and is limited to 25% for this probe.
"""
import argparse
import json
import queue
from pathlib import Path
import sys
import threading
import time

from dsbridge.runtime.paths import current_paths
BASE = current_paths().state
sys.path.insert(0, str(BASE / ".localdeps"))
from dsbridge.core.engine import Engine
from dsbridge.diagnostics.audio_support import _soundcard, _windows_com
from dsbridge.virtual.dualsense.runtime import LocalViiper
from dsbridge.platform.windows.xinput import XInput


class RecordingInput:
    def __init__(self, physical):
        self.xi = XInput()
        self.physical = physical
        self.writes = []
        self.lock = threading.Lock()
    def get_state(self, index):
        return self.xi.get_state(index)
    def set_rumble(self, index, left, right):
        with self.lock:
            self.writes.append((time.monotonic(), left, right))
        return self.xi.set_rumble(index, min(left, .25), min(right, .25)) if self.physical else True
    def close(self):
        self.xi.close()


def wait_started(engine, events):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            kind, value = events.get(timeout=.1)
            if kind == "error":
                raise RuntimeError(value)
            if kind == "status" and value.startswith("已启动"):
                return
        except queue.Empty:
            pass
    raise TimeoutError("Bridge did not start")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--physical", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("Explicit --check required")
    import numpy as np
    xi = RecordingInput(args.physical)
    runtime = LocalViiper(current_paths(), {"host": "127.0.0.1", "port": 3242, "haptics_gain": 1.0})
    events = queue.Queue()
    engine = Engine(xi, events, lambda *_: runtime)
    engine.gain = .25
    report = {"physical_output": args.physical, "test_source": "synthetic 4ch PCM, not a commercial game"}
    try:
        engine.start(0, 1)
        wait_started(engine, events)
        sc = _soundcard()
        with _windows_com():
            audio_deadline = time.monotonic() + 12
            while True:
                outputs = sc.all_speakers()
                candidates = [item for item in outputs if "controller" in item.name.lower() and item.channels >= 4]
                if len(candidates) == 1 or time.monotonic() >= audio_deadline:
                    break
                if runtime.error:
                    raise runtime.error
                time.sleep(.25)
            report["outputs"] = [{"id": item.id, "name": item.name, "channels": item.channels} for item in outputs]
            if len(candidates) != 1:
                raise RuntimeError("Expected exactly one virtual Sony 4-channel render endpoint")
            speaker = candidates[0]
            t = np.arange(4800) / 48000
            native = np.zeros((4800, 4), dtype=np.float32)
            native[:, 2] = .18 * np.sin(2 * np.pi * 80 * t)
            native[:, 3] = .10 * np.sin(2 * np.pi * 120 * t)
            with speaker.player(samplerate=48000, channels=[0, 1, 2, 3], blocksize=480) as player:
                player.play(native)
                player.play(np.zeros((9600, 4), dtype=np.float32))
                time.sleep(.12)
            report["backend"] = runtime.backend.status()
            with xi.lock:
                report["write_count"] = len(xi.writes)
                report["max_feedback"] = [max((row[lane] for row in xi.writes), default=0) for lane in (1, 2)]
                report["latest_feedback"] = xi.writes[-1][1:] if xi.writes else None
            if not report["backend"]["native_haptics_frames"] or not any(report["max_feedback"]):
                raise RuntimeError("Rear haptic PCM did not reach the output bridge")
            if runtime.error:
                raise runtime.error
        from dsbridge.diagnostics.sony_writer import enum_dualsense_outputs, send_rumble
        hid_paths = enum_dualsense_outputs()
        report["virtual_sony_hid_count"] = len(hid_paths)
        if len(hid_paths) != 1:
            raise RuntimeError("Expected exactly our one virtual USB/IP Sony HID")
        started = time.monotonic()
        try:
            report["hid_write"] = send_rumble(hid_paths[0], 64, 32)
            time.sleep(.12)
            with xi.lock:
                writes = [row for row in xi.writes if row[0] >= started]
            report["hid_rumble_peak"] = [max((row[lane] for row in writes), default=0) for lane in (1, 2)]
            for actual, expected in zip(report["hid_rumble_peak"], (64 / 255 * .25, 32 / 255 * .25)):
                if abs(actual - expected) > .003:
                    raise RuntimeError("Sony HID rumble was not converted to the expected physical motor strength")
        finally:
            send_rumble(hid_paths[0], 0, 0)
            time.sleep(.06)
        report["backend_after_hid"] = runtime.backend.status()
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        engine.stop()
        if engine.thread:
            engine.thread.join(timeout=8)
        with xi.lock:
            report["shutdown_feedback"] = xi.writes[-1][1:] if xi.writes else None
        xi.close()
        (BASE / "feedback-integration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
