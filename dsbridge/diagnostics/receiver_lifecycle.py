"""Packaged receiver lifecycle check; gain is zero, no test haptics injected."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
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
    if not getattr(sys, "frozen", False):
        raise RuntimeError("接收器生命周期实机自检仅能由打包程序运行")
    if case not in ("normal", "start-failure", "runtime-failure", "crash"):
        raise ValueError("未知接收器生命周期测试")
    from dsbridge.application.context import ApplicationContext
    from dsbridge.core.engine import Engine
    from dsbridge.controllers.flydigi.apex6.recovery import save_json
    from dsbridge.diagnostics.hidhide import read_config
    from dsbridge.platform.windows.audio import WindowsAudio
    from dsbridge.runtime.paths import as_paths
    paths = as_paths(base)
    base = paths.state
    output = base / ("input-lifecycle-" + case + ".json")
    context = ApplicationContext(paths)
    context.recover()
    xi, events = context.create_input(5), queue.Queue()
    slots = [index for index in xi.slots if xi.get_state(index) is not None]
    if len(slots) != 1:
        xi.close()
        raise RuntimeError("自动恢复实机测试需要连接唯一且已唤醒的实体手柄")
    backend = context.create_backend(5, input_isolated=True)
    backend.set_gain(0)
    if backend.isolation is not None:
        xi.close()
        raise RuntimeError("生命周期自检不能使用旧输入隐藏路径")
    transition = None
    cleanup_order = []
    original_backend_stop = backend.stop
    def stop_backend():
        cleanup_order.append("backend")
        return original_backend_stop()
    backend.stop = stop_backend

    def create_transition(input_port, index, mode, event_queue):
        nonlocal transition
        transition = context.create_transition(input_port, index, mode, event_queue)
        if transition is None:
            raise RuntimeError("生命周期自检必须使用生产热切换路径")
        # Keep fault injection on this backend, constructed with production
        # settings but without the obsolete nested receiver-only hide lease.
        transition.create_backend = lambda: backend
        original_stop = transition.stop
        def stop_transition():
            cleanup_order.append("transition")
            return original_stop()
        transition.stop = stop_transition
        return transition

    engine = Engine(xi, events, lambda *_: backend, profiles=context.registry.profiles,
                    transition_factory=create_transition)
    engine.gain = 0
    if case == "start-failure":
        def fail(*_args):
            raise RuntimeError("显式启动失败自检")
        backend.viiper.start = fail
        backend.viiper.start_cancellable = fail
    report = dict(case=case, process_id=os.getpid(), phase="starting", nonzero_test_haptics=False)
    report["before"] = visibility(paths)
    report["config_before"] = read_config()
    with WindowsAudio() as audio:
        report["audio_before"] = audio.snapshot()["defaults"]
    save_json(output, report)
    try:
        engine.start(slots[0], 5)
        deadline = time.monotonic() + 90
        started, errors = False, []
        while time.monotonic() < deadline:
            try:
                kind, value = events.get(timeout=min(1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
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
            switch = transition.status()
            guard = switch.get("lease", {})
            if switch.get("phase") != "ready" or guard.get("phase") != "ready" or not guard.get("guard_pid"):
                raise RuntimeError("生产热切换恢复进程尚未确认就绪")
            report.update(phase="running", guard_pid=guard["guard_pid"], input_transition=switch,
                          server_pid=backend.viiper.process.pid if backend.viiper.process else None)
            report["running_status"] = backend.status()
            save_json(output, report)
            if case == "crash":
                os._exit(73)  # Intentional termination of this test instance only.
            if case == "runtime-failure":
                backend._failure = RuntimeError("显式运行失败自检")
                engine.thread.join(90)
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        engine.stop()
        if engine.thread:
            engine.thread.join(90)
        if not engine.running:
            xi.close()
        report["engine_stopped"] = not engine.running
        report["after"] = visibility(paths)
        report["config_after"] = read_config()
        report["status_after"] = backend.status()
        report["input_transition"] = transition.status() if transition else None
        report["cleanup_order"] = list(cleanup_order)
        switch = report["input_transition"] or {}
        guard = switch.get("lease", {})
        report["guard_pid"] = guard.get("guard_pid", report.get("guard_pid"))
        restored = (switch.get("refresh_requested") is True and switch.get("phase") == "stopped"
                    and guard.get("phase") == "finished" and guard.get("restored") is True
                    and guard.get("input_visibility_restored") is True and guard.get("refresh_pending") is False)
        stop_order_ok = ("backend" in cleanup_order and "transition" in cleanup_order
                         and cleanup_order.index("backend") < cleanup_order.index("transition"))
        with WindowsAudio() as audio:
            report["audio_after"] = audio.snapshot()["defaults"]
        report["passed"] = (not report.get("error") and not engine.running
                            and restored and stop_order_ok
                            and all(value == 1 for value in counts(report["after"]).values())
                            and report["config_after"]["hidden_devices"] == report["config_before"]["hidden_devices"]
                            and report["audio_after"] == report["audio_before"]
                            and not any(report["status_after"]["receiver"]["motor_nonzero_writes"]))
        report["phase"] = "finished"
        save_json(output, report)
    if not report["passed"]:
        raise RuntimeError("输入自动恢复实机检查未通过")
