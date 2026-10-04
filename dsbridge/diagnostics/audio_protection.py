"""Verify real USB/IP audio arrival/removal and rear haptics, without motors."""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

from dsbridge.runtime.paths import current_paths
BASE = current_paths().state
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(BASE / ".localdeps"))

from dsbridge.diagnostics.audio_support import _soundcard, _windows_com
from dsbridge.virtual.dualsense.runtime import LocalViiper
from dsbridge.platform.windows.audio import WindowsAudio, is_sony_audio


def assert_defaults_preserved(before, after):
    for key, endpoint in before["defaults"].items():
        actual = after["defaults"].get(key)
        if endpoint and (not actual or endpoint["id"] != actual["id"]):
            raise RuntimeError("Default audio changed during virtual controller test: " + key)
        if is_sony_audio(actual):
            raise RuntimeError("DualSense has become a default audio endpoint: " + key)


def main(argv=None):
    parser = argparse.ArgumentParser(description="默认音频保护实机验证，不向实体手柄输出震动")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("Explicit --check required")
    report = {"physical_rumble": False, "microphone_recording": False}
    runtime = LocalViiper(current_paths(), {"host": "127.0.0.1", "port": 3242, "haptics_gain": 1.0})
    collector_stop = threading.Event()
    collector = None
    peak = [0.0, 0.0]
    collector_error = []
    try:
        with WindowsAudio() as audio:
            before = audio.snapshot()
        if any(is_sony_audio(item) for item in before["defaults"].values()):
            raise RuntimeError("Please restore computer audio before running this preservation check")
        report["before"] = before
        existing_ids = {item["id"] for item in before["endpoints"]}
        runtime.start()
        time.sleep(0.4)
        sc = _soundcard()
        with _windows_com():
            deadline = time.monotonic() + 12
            while True:
                speakers = sc.all_speakers()
                candidates = [item for item in speakers if "dualsense" in item.name.casefold()
                              and item.id not in existing_ids and item.channels == 4]
                if len(candidates) == 1:
                    break
                if runtime.error:
                    raise runtime.error
                if time.monotonic() >= deadline:
                    raise RuntimeError("Could not uniquely identify this test's new four-channel Sony endpoint")
                time.sleep(.2)
            target = candidates[0]
            with WindowsAudio() as audio:
                report["during"] = audio.snapshot()
            assert_defaults_preserved(before, report["during"])
            report["haptics_endpoint"] = {"id": target.id, "name": target.name, "channels": target.channels}

            def collect():
                try:
                    while not collector_stop.is_set():
                        runtime.update({})
                        if runtime.error:
                            raise runtime.error
                        value = runtime.poll_feedback()
                        if value:
                            for lane in (0, 1):
                                peak[lane] = max(peak[lane], value[lane])
                        collector_stop.wait(.004)
                except Exception as exc:
                    collector_error.append(str(exc))

            collector = threading.Thread(target=collect, name="Audio protection test feedback", daemon=True)
            collector.start()
            import numpy as np
            t = np.arange(9600) / 48000
            native = np.zeros((9600, 4), dtype=np.float32)
            native[:, 2] = .08 * np.sin(2 * np.pi * 80 * t)
            native[:, 3] = .06 * np.sin(2 * np.pi * 120 * t)
            with target.player(samplerate=48000, channels=[0, 1, 2, 3], blocksize=480) as player:
                player.play(native)
                player.play(np.zeros((4800, 4), dtype=np.float32))
                time.sleep(.12)
        if collector_error:
            raise RuntimeError(collector_error[0])
        report["runtime"] = runtime.status()
        report["native_feedback_peak"] = peak
        if not report["runtime"]["native_haptics_frames"] or not all(value > 0 for value in peak):
            raise RuntimeError("Native rear-channel haptics did not survive audio-default protection")
        if runtime.error:
            raise runtime.error
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        collector_stop.set()
        if collector:
            collector.join(2)
        try:
            runtime.stop()
            time.sleep(.3)
            with WindowsAudio() as audio:
                report["after"] = audio.snapshot()
            if report.get("before"):
                assert_defaults_preserved(report["before"], report["after"])
        except Exception as exc:
            report.setdefault("error", str(exc))
        report["result"] = "failed" if report.get("error") else "passed"
        (BASE / "audio-protection-integration.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("error"):
        raise RuntimeError(report["error"])


if __name__ == "__main__":
    main()
