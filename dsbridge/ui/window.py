"""Tk views for DS bridging; application services are supplied by context."""
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.engine import Engine
from dsbridge.core.session_log import FeedbackSessionLog
from dsbridge.core.modes import MODES, MODE_IDS, DETAILS
from dsbridge.ui.presentation import feedback_text
from dsbridge.runtime.preferences import GainPreferences, gain_percent, MIN_GAIN_PERCENT, MAX_GAIN_PERCENT
from dsbridge import __version__

def run_gui(smoke=False, callback_test=False, initial_mode=1, receiver_close_test=False, setup=False, *, context=None):
    from dsbridge.application.context import ApplicationContext
    context = context or ApplicationContext.default()
    profiles = context.registry.profiles
    MODES = {key: p.label for key, p in profiles.items()}
    MODE_IDS = {label: key for key, label in MODES.items()}
    DETAILS = {key: p.detail for key, p in profiles.items()}
    BASE = context.paths.state
    APP_DIAGNOSTICS = context.diagnostics
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("八爪鱼震动桥 " + __version__)
    root.geometry("850x765")
    root.minsize(760, 725)
    if smoke:
        root.withdraw()
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TLabel", font=("Microsoft YaHei UI", 10))
    style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="八爪鱼震动桥", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
    ttk.Label(frame, text="PS5 DualSense 桥接 · 自动识别蓝牙 / 接收器 / USB", foreground="#58677b").pack(anchor="w", pady=(3, 12))

    events = queue.Queue()
    xi = context.create_input(initial_mode)
    engine = Engine(xi, events, context.create_backend, diagnostics=context.diagnostics, profiles=profiles)
    session_log = FeedbackSessionLog(BASE / "feedback-session.json")
    session_log_failed = False
    mode_var = tk.StringVar(value=MODES[initial_mode])
    device_var = tk.StringVar()
    status_var = tk.StringVar(value="等待启动。请先检测手柄。")
    detail_var = tk.StringVar(value=DETAILS[initial_mode])
    connection_var = tk.StringVar(value="连接方式：请选择手柄。")
    selected_connection = ControllerConnection()
    connection_generation = 0
    connection_pending = False
    connection_future = None
    connection_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Controller connection")
    automatic_modes = set(profiles).issubset({1, 5})
    meter_var = tk.StringVar(value="输入：—    反馈：—")
    ps5_var = tk.StringVar(value="")
    audio_status_var = tk.StringVar(value="默认音频：正在检查扬声器和麦克风…")
    preferences = GainPreferences(BASE)
    gain_var = tk.DoubleVar(value=0 if receiver_close_test else preferences.percent)
    engine.gain = gain_var.get() / 100
    gain_save_timer = None
    gain_save_error = None
    devices = {}
    audio_repair_thread = None
    disconnect_notice = None

    row = ttk.Frame(frame)
    row.pack(fill="x")
    ttk.Label(row, text="手柄输入").pack(side="left")
    device_box = ttk.Combobox(row, textvariable=device_var, state="readonly", width=43)
    device_box.pack(side="left", padx=12)

    def log(message):
        log_box.configure(state="normal")
        log_box.insert("end", time.strftime("%H:%M:%S ") + message + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def detect():
        nonlocal disconnect_notice
        if engine.running:
            return
        disconnect_notice = None
        previous = device_var.get()
        devices.clear()
        xi.select(MODE_IDS[mode_var.get()])
        for i in xi.slots:
            state = xi.get_state(i)
            if state is not None:
                devices[xi.label(i)] = i
        device_box["values"] = list(devices)
        if devices:
            device_var.set(previous if previous in devices else next(iter(devices)))
            status_var.set("检测到 " + str(len(devices)) + " 个手柄。")
        else:
            device_var.set("")
            status_var.set("未检测到手柄。请连接蓝牙、2.4G 接收器或 USB，并保持手柄处于 PC/XInput 模式。")
        log(status_var.get())
        detect_selected()

    def detect_selected(_event=None, *, action=None):
        nonlocal connection_generation, connection_pending, connection_future, selected_connection
        if engine.running or closing:
            return
        connection_generation += 1
        generation = connection_generation
        if connection_future:
            connection_future.cancel()
        label = device_var.get()
        if label not in devices:
            connection_pending = False
            selected_connection = ControllerConnection("disconnected")
            connection_var.set("连接方式：未检测到在线手柄。")
            start_button.configure(state="disabled")
            test_button.configure(state="disabled")
            return
        slot, mode = devices[label], MODE_IDS[mode_var.get()]
        connection_pending = True
        connection_var.set("连接方式：正在检测所选手柄…")
        start_button.configure(state="disabled")
        test_button.configure(state="disabled")
        def read_connection():
            try:
                value = context.detect_connection(mode, slot)
            except Exception as exc:
                value = ControllerConnection(detail="检测未完成：" + str(exc))
            events.put(("connection", (generation, label, mode, value, action)))
        connection_future = connection_worker.submit(read_connection)

    device_box.bind("<<ComboboxSelected>>", detect_selected)

    detect_button = ttk.Button(row, text="检测", command=detect)
    detect_button.pack(side="left")
    ttk.Label(frame, textvariable=connection_var, foreground="#195d98", wraplength=780).pack(anchor="w", pady=(8, 0))
    ttk.Label(frame, text="PS5 桥接方式（根据所选手柄自动匹配）").pack(anchor="w", pady=(10, 5))
    mode_box = ttk.Combobox(frame, textvariable=mode_var, values=tuple(MODES.values()), state="disabled" if automatic_modes else "readonly")
    mode_box.pack(fill="x")
    ttk.Label(frame, textvariable=detail_var, wraplength=780, foreground="#43516a").pack(anchor="w", pady=8)

    def mode_changed(_event=None):
        idx = MODE_IDS[mode_var.get()]
        detail_var.set(DETAILS[idx])
        test_button.configure(text=profiles[idx].test_label)
        if not smoke:
            detect()

    mode_box.bind("<<ComboboxSelected>>", mode_changed)
    gain_row = ttk.Frame(frame)
    gain_row.pack(fill="x", pady=(18, 8))
    gain_label = ttk.Label(gain_row, text="转换强度 " + str(round(gain_var.get())) + "%")
    gain_label.pack(side="left")

    gain_help = "调节 DS 桥接后的震动强度；反馈内容由游戏提供。"

    def save_gain(*, closing_window=False):
        nonlocal gain_save_timer, gain_save_error
        if gain_save_timer is not None:
            root.after_cancel(gain_save_timer)
            gain_save_timer = None
        # A diagnostic's temporary zero must never replace the user's setting.
        if receiver_close_test:
            return
        try:
            preferences.save(gain_percent(gain_var.get()))
        except OSError as exc:
            message = "转换强度保存失败：" + str(exc)
            gain_help_label.configure(text="强度尚未保存，下次打开可能恢复原值。详情见运行日志。", foreground="#b34525")
            if message != gain_save_error:
                log(message)
                if APP_DIAGNOSTICS:
                    APP_DIAGNOSTICS.event("gain_settings_save_failed", error=str(exc))
            gain_save_error = message
            if closing_window and not smoke:
                messagebox.showwarning("强度未保存", "本次转换强度未能保存，下次打开可能恢复原值。\n" + str(exc))
            return
        if gain_save_error:
            log("转换强度已保存。")
        gain_save_error = None
        gain_help_label.configure(text=gain_help, foreground="#66758a")

    def gain_changed(value):
        nonlocal gain_save_timer
        percent = gain_percent(float(value))
        gain_var.set(percent)
        engine.gain = percent / 100
        gain_label.configure(text="转换强度 " + str(percent) + "%")
        if gain_save_timer is not None:
            root.after_cancel(gain_save_timer)
        gain_save_timer = root.after(500, save_gain)

    ttk.Scale(gain_row, from_=MIN_GAIN_PERCENT, to=MAX_GAIN_PERCENT, variable=gain_var, command=gain_changed).pack(side="left", fill="x", expand=True, padx=16)
    gain_help_label = ttk.Label(frame, text=gain_help, foreground="#66758a", wraplength=780)
    gain_help_label.pack(anchor="w")
    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=14)

    def set_busy(busy):
        dependency_panel.set_conversion_busy(busy)
        device_box.configure(state="disabled" if busy else "readonly")
        mode_box.configure(state="disabled" if busy or automatic_modes else "readonly")
        detect_button.configure(state="disabled" if busy else "normal")
        ready = not busy and not connection_pending and selected_connection.kind not in ("disconnected", "virtual")
        start_button.configure(state="normal" if ready else "disabled")
        test_button.configure(state="normal" if ready else "disabled")
        stop_button.configure(state="normal" if busy else "disabled")

    def start():
        # Re-read before starting: switching cables may reuse the same slot.
        detect_selected(action="start")

    def start_checked():
        nonlocal disconnect_notice
        if not dependency_panel.guard_start(MODE_IDS[mode_var.get()]):
            return
        if device_var.get() not in devices:
            messagebox.showerror("未连接", "请先连接手柄并检测。")
            return
        try:
            mode = MODE_IDS[mode_var.get()]
            disconnect_notice = None
            engine.gain = gain_var.get() / 100
            engine.start(devices[device_var.get()], mode)
            set_busy(True)
            status_var.set("正在启动…")
            log(profiles[mode].start_notice)
        except Exception as exc:
            if receiver_close_test:
                raise
            messagebox.showerror("启动失败", str(exc))

    def stop():
        engine.stop()
        status_var.set("正在停止…")

    def test_motors():
        detect_selected(action="test")

    def test_motors_checked():
        if dependency_panel.installing:
            dependency_panel.open()
            return
        if device_var.get() not in devices:
            return
        index = devices[device_var.get()]
        test_mode = MODE_IDS[mode_var.get()]
        set_busy(True)
        def test():
            try:
                context.test_motors(test_mode, xi, index, lambda message: events.put(("log", message)))
            except Exception as exc:
                events.put(("error", str(exc)))
            finally:
                xi.close()
                events.put(("test_done", None))
        threading.Thread(target=test, daemon=True).start()

    start_button = ttk.Button(controls, text="启动", command=start)
    start_button.pack(side="left")
    stop_button = ttk.Button(controls, text="停止转换", command=stop, state="disabled")
    stop_button.pack(side="left", padx=8)
    test_button = ttk.Button(controls, text=profiles[initial_mode].test_label, command=test_motors)
    test_button.pack(side="left")
    ttk.Button(controls, text="使用说明", command=lambda: os.startfile(str(context.paths.assets / "使用说明.txt"))).pack(side="right")

    def repair_audio():
        nonlocal audio_repair_thread
        if audio_repair_thread and audio_repair_thread.is_alive():
            return
        repair_button.configure(state="disabled")
        def repair():
            try:
                report = context.repair_audio()
                events.put(("audio", report))
            except Exception as exc:
                events.put(("audio_error", str(exc)))
        audio_repair_thread = threading.Thread(target=repair, name="Restore computer audio", daemon=True)
        audio_repair_thread.start()

    repair_button = ttk.Button(controls, text="恢复电脑声音", command=repair_audio)
    repair_button.pack(side="right", padx=8)
    ttk.Label(frame, textvariable=status_var, foreground="#195d98", wraplength=780).pack(anchor="w", pady=(0, 8))
    ttk.Label(frame, textvariable=meter_var).pack(anchor="w")
    ttk.Label(frame, textvariable=ps5_var, foreground="#58677b").pack(anchor="w")
    ttk.Label(frame, textvariable=audio_status_var, foreground="#58677b", wraplength=780).pack(anchor="w")
    ttk.Label(frame, text="运行日志").pack(anchor="w", pady=(15, 5))
    log_box = tk.Text(frame, height=7, font=("Microsoft YaHei UI", 9), bg="#f1f4f8", relief="flat", wrap="word", state="disabled")
    log_box.pack(fill="both", expand=True)
    from dsbridge.ui.dependencies import DependencyPanel
    dependency_panel = DependencyPanel(frame, context.paths, log=log, manager=context.dependencies, profiles=profiles)
    closing = False
    pump_timer = None
    gui_errors = []
    if preferences.load_error:
        log("上次转换强度无法读取，已使用默认 70%：" + preferences.load_error)
        gain_help_label.configure(text="上次强度设置无法读取，已使用默认 70%。", foreground="#b34525")
        if APP_DIAGNOSTICS:
            APP_DIAGNOSTICS.event("gain_settings_load_failed", error=preferences.load_error)

    def destroy_window():
        nonlocal pump_timer
        if pump_timer is not None:
            root.after_cancel(pump_timer)
            pump_timer = None
        xi.close()
        root.destroy()

    def save_session(record, *, final=False):
        record = dict(record)
        record["ui"] = {"state": root.state(), "mapped": bool(root.winfo_ismapped()),
                        "closing": closing}
        session_log.write(record, final=final)

    def report_gui_exception(exc_type, exc, tb):
        # Windowed Python has no stderr. Persist Tk callback failures and keep
        # the existing window usable instead of dropping its update callback.
        if APP_DIAGNOSTICS:
            APP_DIAGNOSTICS.exception("tk_callback", exc_type, exc, tb)
        gui_errors.append(str(exc))
        engine.stop()
        try:
            status_var.set("界面发生错误，转换正在停止。详情已保存到 logs/app-events.jsonl。")
            log(str(exc))
        except tk.TclError:
            pass

    root.report_callback_exception = report_gui_exception

    def pump_once():
        nonlocal closing, session_log_failed, connection_pending, selected_connection, disconnect_notice
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "connection":
                    generation, label, previous_mode, value, action = value
                    if generation != connection_generation or label != device_var.get():
                        continue
                    connection_pending = False
                    selected_connection = value
                    connection_var.set("连接方式：" + value.label + ("。" + value.detail if value.detail else ""))
                    mode = context.connection_profile(previous_mode, value)
                    mode_var.set(MODES[mode])
                    detail_var.set(DETAILS[mode])
                    test_button.configure(text=profiles[mode].test_label)
                    xi.select(mode)
                    set_busy(False)
                    if APP_DIAGNOSTICS:
                        APP_DIAGNOSTICS.event("controller_connection", kind=value.kind, profile=mode)
                    if value.kind in ("disconnected", "virtual"):
                        status_var.set(value.detail or "请选择在线实体手柄。")
                    elif action and not closing:
                        (start_checked if action == "start" else test_motors_checked)()
                elif kind == "live":
                    state, rumble = value["state"], value["rumble"]
                    metadata = value.get("ps5")
                    meter, feedback, protected = feedback_text(state, rumble, metadata)
                    meter_var.set(meter)
                    ps5_var.set(feedback)
                    if protected:
                        audio_status_var.set("默认音频保护：已开启（扬声器和麦克风保持使用电脑设备）")
                    try:
                        save_session(value["session"])
                    except OSError as exc:
                        if not session_log_failed:
                            log("反馈统计保存失败：" + str(exc))
                            session_log_failed = True
                elif kind == "session_end":
                    try:
                        save_session(value, final=True)
                    except OSError as exc:
                        log("反馈统计保存失败：" + str(exc))
                elif kind == "error":
                    status_var.set("运行失败：" + value)
                    log(status_var.get())
                elif kind == "disconnected":
                    disconnect_notice = str(value) + " 重新连接后，请点击“检测”，再启动转换。"
                    selected_connection = ControllerConnection("disconnected")
                    connection_var.set("连接方式：手柄已断开。")
                    status_var.set(disconnect_notice)
                    meter_var.set("输入：已断开    反馈：正在停止")
                    ps5_var.set("")
                    log(disconnect_notice)
                elif kind == "status":
                    if not disconnect_notice:
                        status_var.set(value)
                    log(value)
                elif kind in ("stopped", "test_done"):
                    set_busy(False)
                    ps5_var.set("")
                    if not disconnect_notice and not status_var.get().startswith("运行失败"):
                        status_var.set(profiles[MODE_IDS[mode_var.get()]].stopped_notice)
                    meter_var.set("输入：已断开    反馈：已停振" if disconnect_notice else "输入：—    反馈：已停振")
                elif kind == "log":
                    log(value)
                elif kind == "audio":
                    defaults = value["after"]["defaults"]
                    output = defaults.get("render:0")
                    microphone = defaults.get("capture:0")
                    audio_status_var.set("默认音频：" + (output["name"] if output else "无可用输出")
                                         + " / " + (microphone["name"] if microphone else "无可用麦克风"))
                    log("默认扬声器和麦克风已检查；恢复了 " + str(len(value["changes"])) + " 项被 DS 抢占的设置。")
                    repair_button.configure(state="normal")
                elif kind == "audio_error":
                    audio_status_var.set("默认音频恢复失败：" + value)
                    log(audio_status_var.get())
                    repair_button.configure(state="normal")
        except queue.Empty:
            pass
        audio_busy = audio_repair_thread and audio_repair_thread.is_alive()
        if closing and not engine.running and not audio_busy and not connection_pending and str(stop_button["state"]) == "disabled":
            destroy_window()
            return False
        return True

    def pump():
        nonlocal pump_timer
        alive = True
        try:
            alive = pump_once()
        finally:
            # A failed update must not silently remove the only UI timer.
            if alive:
                try:
                    if root.winfo_exists():
                        pump_timer = root.after(100, pump)
                except tk.TclError:
                    pass

    def close():
        nonlocal closing
        if dependency_panel.installing:
            dependency_panel.open()
            status_var.set("驱动安装仍在进行，请等待安装结果后关闭。")
            return
        if APP_DIAGNOSTICS:
            APP_DIAGNOSTICS.event("window_close_requested", engine_running=engine.running)
        save_gain(closing_window=True)
        closing = True
        engine.stop()
        audio_busy = audio_repair_thread and audio_repair_thread.is_alive()
        if not engine.running and not audio_busy and not connection_pending and str(stop_button["state"]) == "disabled":
            destroy_window()
        else:
            status_var.set("正在停振并释放设备…")

    root.protocol("WM_DELETE_WINDOW", close)
    detect()
    pump()
    if not smoke:
        repair_audio()
    if setup:
        dependency_panel.open()
    if smoke:
        root.update_idletasks()
        report = {"gui": "ok", "title": root.title(), "devices": list(devices), "mode": mode_var.get(), "requested_size": [root.winfo_reqwidth(), root.winfo_reqheight()]}
        report["available_modes"] = list(mode_box["values"])
        report["mode_selections"] = []
        for label in mode_box["values"]:
            mode_var.set(label)
            mode_box.event_generate("<<ComboboxSelected>>")
            root.update_idletasks()
            report["mode_selections"].append({"name": label, "id": MODE_IDS[label],
                                               "detail": detail_var.get(), "test_button": test_button["text"]})
        mode_var.set(MODES[initial_mode])
        mode_changed()
        def main_widget_texts(widget):
            texts = []
            if "text" in widget.keys() and widget["text"]:
                texts.append(str(widget["text"]))
            for child in widget.winfo_children():
                texts.extend(main_widget_texts(child))
            return texts
        report["main_controls"] = main_widget_texts(frame)
        report["removed_modules_loaded"] = [name for name in ("audio_haptics", "xbox_ds_backend", "probe_xbox_ds") if name in sys.modules]
        smoke_deadline = time.monotonic() + 25
        def finish_smoke():
            if (dependency_panel.checking or connection_pending) and time.monotonic() < smoke_deadline:
                root.after(100, finish_smoke)
                return
            report["dependencies"] = [row.to_dict() for row in dependency_panel.rows or []]
            report["connection"] = selected_connection.to_dict()
            report["connection_text"] = connection_var.get()
            report["selected_profile"] = MODE_IDS[mode_var.get()]
            report["connection_check_finished"] = not connection_pending
            report["automatic_modes"] = automatic_modes
            report["gain_percent"] = round(gain_var.get())
            report["engine_gain"] = engine.gain
            report["gain_label"] = str(gain_label.cget("text"))
            report["gain_settings_status"] = str(gain_help_label.cget("text"))
            report["dependency_check_finished"] = not dependency_panel.checking and len(report["dependencies"]) == len(context.dependencies.packages)
            report["setup_open"] = bool(dependency_panel.dialog)
            if dependency_panel.dialog:
                report["install_button"] = str(dependency_panel.install_button["state"])
                report["setup_requested_size"] = [dependency_panel.dialog.winfo_reqwidth(), dependency_panel.dialog.winfo_reqheight()]
            (BASE / "gui-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if not receiver_close_test:
                close()
        root.after(250, finish_smoke)
        if callback_test:
            def inject_callback_failure():
                raise RuntimeError("显式界面异常自检")
            def check_callback_recovery():
                report = {"callback_caught": gui_errors == ["显式界面异常自检"],
                          "window_alive": bool(root.winfo_exists()),
                          "engine_running": engine.running}
                (BASE / "gui-callback-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            root.after(30, inject_callback_failure)
            root.after(150, check_callback_recovery)
    lifecycle = {"stage": 0, "stopped_input_counts": None, "error": None}
    if receiver_close_test:
        # Exercise the actual buttons and close handler, twice in one window.
        # Zero gain and no injected feedback keep this test physically silent.
        from dsbridge.diagnostics.receiver_lifecycle import visibility, counts
        from dsbridge.controllers.flydigi.apex6.recovery import save_json
        deadline = time.monotonic() + 45
        def lifecycle_step():
            try:
                if gui_errors or status_var.get().startswith("运行失败"):
                    raise RuntimeError(status_var.get())
                if time.monotonic() > deadline:
                    raise RuntimeError("窗口停止及退出恢复测试超时")
                if lifecycle["stage"] == 0:
                    if dependency_panel.checking or dependency_panel.rows is None:
                        root.after(100, lifecycle_step)
                        return
                    start()
                    lifecycle["stage"] = 1
                elif lifecycle["stage"] == 1 and status_var.get().startswith("已启动"):
                    stop()
                    lifecycle["stage"] = 2
                elif lifecycle["stage"] == 2 and not engine.running and str(start_button["state"]) == "normal":
                    lifecycle["stopped_input_counts"] = counts(visibility(context.paths))
                    if not all(value == 1 for value in lifecycle["stopped_input_counts"].values()):
                        raise RuntimeError("点击停止后普通输入未恢复")
                    start()
                    lifecycle["stage"] = 3
                elif lifecycle["stage"] == 3 and status_var.get().startswith("已启动"):
                    lifecycle["stage"] = 4
                    close()
                    return
            except Exception as exc:
                lifecycle["error"] = str(exc)
                close()
                return
            root.after(100, lifecycle_step)
        root.after(250, lifecycle_step)
    if APP_DIAGNOSTICS:
        APP_DIAGNOSTICS.event("window_opened", state=root.state(), smoke=smoke)
    try:
        root.mainloop()
    finally:
        # Also persist after an unexpected mainloop exit, without reading a
        # destroyed Tk variable or trying to display a dialog.
        if not receiver_close_test:
            try:
                preferences.save(gain_percent(engine.gain * 100))
            except OSError as exc:
                if APP_DIAGNOSTICS:
                    APP_DIAGNOSTICS.event("gain_settings_save_failed", error=str(exc))
        connection_worker.shutdown(wait=True, cancel_futures=True)
        # Also clean up if Tcl exits its event loop through an unexpected path.
        engine.stop()
        if engine.thread:
            engine.thread.join(20)
        if audio_repair_thread:
            audio_repair_thread.join(5)
        xi.close()
        if APP_DIAGNOSTICS:
            APP_DIAGNOSTICS.event("window_loop_ended", close_requested=closing,
                                  engine_running=engine.running)
        if receiver_close_test:
            lifecycle["closed_input_counts"] = counts(visibility(context.paths))
            lifecycle["passed"] = (lifecycle["stage"] == 4 and not lifecycle["error"] and not engine.running
                                   and all(value == 1 for value in lifecycle["closed_input_counts"].values()))
            save_json(BASE / "receiver-window-lifecycle.json", lifecycle)
            if not lifecycle["passed"]:
                raise RuntimeError(lifecycle["error"] or "窗口退出恢复测试失败")
