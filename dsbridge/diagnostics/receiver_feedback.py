"""Synthetic USB DS audio/HID -> receiver; not a commercial-game claim."""
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import time


def main(base):
    from dsbridge.core.engine import Engine
    from dsbridge.diagnostics.audio_support import _soundcard, _windows_com
    from dsbridge.diagnostics.feedback import RecordingInput, wait_started
    from dsbridge.controllers.flydigi.apex6.bridge import ReceiverFeedback
    from dsbridge.diagnostics.sony_writer import enum_dualsense_outputs, send_rumble, send_trigger_effects
    from dsbridge.platform.windows.audio import WindowsAudio
    import numpy as np

    from dsbridge.runtime.paths import as_paths
    paths = as_paths(base)
    base = paths.state
    input_device = RecordingInput(False)
    slots = [i for i in range(4) if input_device.get_state(i) is not None]
    if len(slots) != 1:
        raise RuntimeError("测试需要唯一的实体 XInput 手柄，且测试程序须在 HidHide 允许列表")
    config = paths.config()
    backend = ReceiverFeedback(paths, config)
    events = queue.Queue()
    engine = Engine(input_device, events, lambda *_: backend)
    engine.gain = .25
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "test_source": "synthetic DS rear audio, adaptive-trigger HID and compatible rumble",
              "physical_output": True}
    def defaults():
        with WindowsAudio() as audio:
            return audio.snapshot()["defaults"]
    path = None
    try:
        report["audio_before"] = defaults()
        engine.start(slots[0], 5)
        wait_started(engine, events)
        hid_deadline = time.monotonic() + 12
        while True:
            paths = enum_dualsense_outputs()
            if len(paths) == 1:
                break
            if len(paths) > 1 or time.monotonic() >= hid_deadline:
                raise RuntimeError("需要唯一的 USB/IP 虚拟 Sony 输出接口")
            if backend.error:
                raise RuntimeError(str(backend.error))
            time.sleep(.05)
        path = paths[0]
        sc = _soundcard()
        with _windows_com():
            deadline = time.monotonic() + 12
            while True:
                outputs = sc.all_speakers()
                candidates = [item for item in outputs if "controller" in item.name.lower() and item.channels >= 4]
                if len(candidates) == 1 or time.monotonic() >= deadline:
                    break
                time.sleep(.2)
            if len(candidates) != 1:
                raise RuntimeError("没有唯一的虚拟 Sony 四声道触觉端点")
            speaker = candidates[0]
            report["sony_audio"] = {"name": speaker.name, "channels": speaker.channels}
            with speaker.player(samplerate=48000, channels=[0, 1, 2, 3], blocksize=480) as player:
                player.play(np.zeros((9600, 4), dtype=np.float32))
                native = np.zeros((14400, 4), dtype=np.float32)
                t = np.arange(14400) / 48000
                native[:, 2] = .20 * np.sin(2 * np.pi * 80 * t)
                native[:, 3] = .16 * np.sin(2 * np.pi * 120 * t)
                player.play(native)
                player.play(np.zeros((14400, 4), dtype=np.float32))
                time.sleep(.1)
                report["native"] = backend.status()
                receiver = report["native"]["receiver"]
                if not report["native"].get("native_haptics_nonzero_frames") or not all(receiver["motor_nonzero_writes"][:2]):
                    raise RuntimeError("DS 原生后两路触觉未到达接收器左右握把")
                before = receiver["nonzero_packets"]
                speaker_only = np.zeros((9600, 4), dtype=np.float32)
                speaker_only[:, :2] = (.05 * np.sin(2 * np.pi * 220 * np.arange(9600) / 48000))[:, None]
                player.play(speaker_only)
                player.play(np.zeros((9600, 4), dtype=np.float32))
                time.sleep(.1)
                report["speaker_excluded"] = backend.status()["receiver"]["nonzero_packets"] == before
                if not report["speaker_excluded"]:
                    raise RuntimeError("扬声器音轨意外进入马达输出")

        effect = bytearray(11)
        effect[0], effect[1:3], effect[3:7], effect[9] = 0x26, b"\xff\3", b"\xff\xff\xff\x3f", 100
        right = bytearray(effect)
        right[9] = 140
        send_trigger_effects(path, bytes(effect), bytes(right))
        time.sleep(.25)
        report["triggers"] = backend.status()
        if not all(report["triggers"]["receiver"]["motor_nonzero_writes"][2:]):
            raise RuntimeError("DS 扳机 HID 效果未到达接收器左右扳机")
        send_trigger_effects(path, bytes(11), bytes(11))
        time.sleep(.08)
        before = backend.status()["receiver"]["motor_nonzero_writes"]
        send_rumble(path, 64, 32)
        time.sleep(.2)
        report["ordinary"] = backend.status()
        after = report["ordinary"]["receiver"]["motor_nonzero_writes"]
        if not all(after[i] > before[i] for i in (0, 1)):
            raise RuntimeError("Sony 普通震动未到达接收器左右握把")
        send_rumble(path, 0, 0)
        time.sleep(.08)
        if backend.error:
            raise RuntimeError(str(backend.error))
        report["audio_during"] = defaults()
        if report["audio_during"] != report["audio_before"]:
            raise RuntimeError("转换期间默认音频设置发生改变")
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        if path:
            try:
                send_trigger_effects(path, bytes(11), bytes(11))
                send_rumble(path, 0, 0)
            except Exception:
                pass
        engine.stop()
        if engine.thread:
            engine.thread.join(25)
        report["engine_stopped"] = not engine.running
        report["shutdown"] = backend.status()
        report["xinput_motor_writes"] = len(input_device.writes)
        report["audio_after"] = defaults()
        report["passed"] = (not report.get("error") and report["engine_stopped"]
                            and report["shutdown"]["receiver"]["restored"]
                            and not report["xinput_motor_writes"]
                            and report["audio_after"] == report.get("audio_before"))
        (base / "receiver-feedback-integration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["passed"]:
        raise RuntimeError("接收器自检未完全通过，详见 receiver-feedback-integration.json")
    return report
