"""Packaged receiver lifecycle check; gain is zero, no test haptics injected."""
import json
import os
from pathlib import Path
import queue
import subprocess
import time


def visibility(base):
    from dsbridge.runtime.paths import as_paths
    paths = as_paths(base)
    helper = paths.assets / "接收器输入检查.exe"
    subprocess.run(paths.command("--check-input-visibility", executable=helper), check=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads((Path(base) / "receiver-input-visibility.json").read_text(encoding="utf-8"))


def counts(value):
    return {name: sum(row["connected"] for row in rows) for name, rows in value["libraries"].items()}


def main(base, case):
    from dsbridge.core.engine import Engine
    from dsbridge.controllers.flydigi.apex6.bridge import ReceiverFeedback
    from dsbridge.controllers.flydigi.apex6.recovery import save_json, recover
    from dsbridge.diagnostics.hidhide import read_config
    from dsbridge.platform.windows.xinput import XInput
    from dsbridge.platform.windows.audio import WindowsAudio
    from dsbridge.runtime.paths import as_paths
    paths = as_paths(base)
    base = paths.state
    output = base / ("input-lifecycle-" + case + ".json")
    recover(base)
    xi, events = XInput(), queue.Queue()
    slots = [index for index in range(4) if xi.get_state(index) is not None]
    if len(slots) != 1:
        raise RuntimeError("自动恢复实机测试需要连接唯一且已唤醒的实体手柄")
    backend = ReceiverFeedback(paths, paths.config())
    engine = Engine(xi, events, lambda *_: backend)
    engine.gain = 0
    if case == "start-failure":
        def fail():
            raise RuntimeError("显式启动失败自检")
        backend.viiper.start = fail
    report = dict(case=case, process_id=os.getpid(), phase="starting", nonzero_test_haptics=False)
    report["before"] = visibility(paths)
    report["config_before"] = read_config()
    with WindowsAudio() as audio:
        report["audio_before"] = audio.snapshot()["defaults"]
    save_json(output, report)
    try:
        engine.start(slots[0], 5)
        deadline = time.monotonic() + 30
        started, errors = False, []
        while time.monotonic() < deadline:
            kind, value = events.get(timeout=10)
            if kind == "status" and value.startswith("已启动"):
                started = True
                break
            if kind == "error":
                errors.append(value)
            if kind == "stopped":
                break
        report["startup_errors"] = errors
        if case == "start-failure":
            if started or not any("显式启动失败自检" in value for value in errors):
                raise RuntimeError("启动失败自检没有按预期触发")
        else:
            if not started:
                raise RuntimeError("；".join(errors) or "转换未启动")
            report["during"] = visibility(paths)
            if any(counts(report["during"]).values()):
                raise RuntimeError("运行中游戏仍能看到实体输入")
            time.sleep(.6)
            if not engine.running or backend.error:
                raise RuntimeError(str(backend.error) or "转换提前停止")
            report.update(phase="running", guard_pid=backend.isolation.process.pid,
                          server_pid=backend.viiper.process.pid if backend.viiper.process else None)
            report["running_status"] = backend.status()
            save_json(output, report)
            if case == "crash":
                os._exit(73)  # Intentional termination of this test instance only.
            if case == "runtime-failure":
                backend._failure = RuntimeError("显式运行失败自检")
                engine.thread.join(30)
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        engine.stop()
        if engine.thread:
            engine.thread.join(35)
        xi.close()
        report["engine_stopped"] = not engine.running
        report["after"] = visibility(paths)
        report["config_after"] = read_config()
        report["status_after"] = backend.status()
        with WindowsAudio() as audio:
            report["audio_after"] = audio.snapshot()["defaults"]
        report["passed"] = (not report.get("error") and not engine.running
                            and all(value == 1 for value in counts(report["after"]).values())
                            and report["config_after"]["hidden_devices"] == report["config_before"]["hidden_devices"]
                            and report["audio_after"] == report["audio_before"]
                            and not any(report["status_after"]["receiver"]["motor_nonzero_writes"]))
        report["phase"] = "finished"
        save_json(output, report)
    if not report["passed"]:
        raise RuntimeError("输入自动恢复实机检查未通过")
