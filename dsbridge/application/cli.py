"""Command entry points; resource setup precedes native imports or GUI creation."""
import argparse
import json
from pathlib import Path
import sys
import time
import traceback
from dsbridge.runtime.paths import RuntimePaths, configure
from dsbridge.application.context import ApplicationContext
from dsbridge.diagnostics.logging import RuntimeDiagnostics

def main(argv, context):
    from dsbridge.platform.windows.xinput import XInput
    from dsbridge.ui.window import run_gui
    BASE = context.paths
    APP_DIAGNOSTICS = context.diagnostics
    parser = argparse.ArgumentParser(description="八爪鱼震动桥")
    parser.add_argument("--data-dir", type=Path, help="指定可写的数据目录；默认使用当前用户的 LocalAppData")
    parser.add_argument("--list-adapters", action="store_true", help="导出已注册手柄适配器及其能力")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--check-dependencies", action="store_true", help="只读检查 USBip 和 HidHide，写入 dependencies.json")
    parser.add_argument("--detect-connections", action="store_true", help="检测在线手柄的蓝牙、接收器或 USB 连接，不启振")
    parser.add_argument("--setup", action="store_true", help="打开依赖检查与安装窗口；安装需点击按钮并确认 Windows 权限提示")
    parser.add_argument("--ui-smoke", action="store_true", help="隐藏窗口检查打包后的 GUI 资源，不启振")
    parser.add_argument("--ui-smoke-callback", action="store_true", help="隐藏窗口验证界面异常被记录并保留窗口，不启振")
    parser.add_argument("--self-test-feedback", action="store_true", help="显式验证 PS5 转发链路，会短时轻震并自动停振")
    parser.add_argument("--self-test-touchpad", action="store_true", help="零强度验证实体 View 到 PS5 触摸板点击；需手动按住和松开三次")
    parser.add_argument("--repair-audio", action="store_true", help="恢复被虚拟DS抢占的默认扬声器和麦克风")
    parser.add_argument("--self-test-audio", action="store_true", help="验证默认音频保护及PS5触觉通道，不驱动实体马达")
    parser.add_argument("--receiver-mode", action="store_true", help="打开 2.4G 四马达 DS 转换；原蓝牙模式保留")
    parser.add_argument("--receiver-motor-test", action="store_true", help="接收器四路短时轻震，结束后核对原设置恢复")
    parser.add_argument("--hidhide-status", action="store_true", help="只读取 HidHide 设置到 hidhide-status.json")
    parser.add_argument("--self-test-receiver", action="store_true", help="测试 DS 触觉、扳机和普通震动到接收器；会短时轻震")
    parser.add_argument("--check-input-visibility", action="store_true", help="独立读取两套 XInput 可见性，不输出震动")
    parser.add_argument("--receiver-input-guard", nargs=2, metavar=("TOKEN", "PID"), help=argparse.SUPPRESS)
    parser.add_argument("--recover-receiver-input", action="store_true", help="自动恢复中断会话留下的接收器输入隐藏")
    parser.add_argument("--hot-switch-input-guard", nargs=2, metavar=("TOKEN", "PID"), help=argparse.SUPPRESS)
    parser.add_argument("--recover-hot-switch", action="store_true", help="恢复中断的手柄输入热切换")
    parser.add_argument("--check-hot-switch", action="store_true", help="只读检查自动热切换组件")
    parser.add_argument("--install-hot-switch", action="store_true", help="安装自动热切换组件；首次需要 Windows 权限确认")
    parser.add_argument("--self-test-hot-switch", action="store_true", help="零强度验证已打开输入连接的自动切换与恢复")
    parser.add_argument("--hot-switch-observer", metavar="TOKEN", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-input-lifecycle", choices=("normal", "start-failure", "runtime-failure", "crash"), help="零强度检查接收器自动隐藏及恢复；crash 会结束本次测试进程")
    parser.add_argument("--self-test-receiver-close", action="store_true", help="零强度验证窗口启动、停止、再次启动及关闭后的输入恢复")
    parser.add_argument("--motor-test", type=int, metavar="SLOT", help="显式测试槽位0..3，轻震后停振")
    args = parser.parse_args(argv)
    if args.detect_connections:
        xi = XInput()
        try:
            rows = [{"slot": i, "identity": xi.identity(i), "connection": context.resolve_connection(xi.connection(i)).to_dict()}
                    for i in range(4) if xi.get_state(i) is not None]
            (BASE / "controller-connections.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            xi.close()
    elif args.list_adapters:
        from dsbridge.dependencies.manager import write_report
        write_report(BASE / "adapters-report.json", context.registry.describe())
    elif args.check_dependencies:
        from dsbridge.dependencies.manager import DependencyManager, write_report
        write_report(BASE / "dependencies.json", {"dependencies": [row.to_dict() for row in context.dependencies.check()]})
    elif args.hot_switch_observer:
        from dsbridge.diagnostics.hot_switch import observer_main
        raise SystemExit(observer_main(BASE, args.hot_switch_observer))
    elif args.self_test_hot_switch:
        from dsbridge.diagnostics.hot_switch import run_audit
        run_audit(context)
    elif args.hot_switch_input_guard:
        from dsbridge.platform.windows.hot_switch_lease import guard_main
        raise SystemExit(guard_main(BASE, args.hot_switch_input_guard[0], int(args.hot_switch_input_guard[1])))
    elif args.recover_hot_switch:
        from dsbridge.platform.windows.hot_switch_lease import recover
        from dsbridge.application.context import require_input_recovered
        from dsbridge.dependencies.manager import write_report
        recovery = recover(BASE)
        write_report(BASE / "hot-switch-recovery-result.json", recovery)
        require_input_recovered(recovery)
    elif args.check_hot_switch:
        from dsbridge.platform.windows.hot_switch_service import HotSwitchClient
        from dsbridge.dependencies.manager import write_report
        write_report(BASE / "hot-switch-status.json", HotSwitchClient(BASE).status())
    elif args.install_hot_switch:
        from dsbridge.platform.windows.hot_switch_setup import install_hot_switch_service
        from dsbridge.dependencies.manager import write_report
        write_report(BASE / "hot-switch-status.json", install_hot_switch_service(BASE, lambda message: None, lambda: None))
    elif args.receiver_input_guard:
        from dsbridge.controllers.flydigi.apex6.recovery import guard_main
        raise SystemExit(guard_main(BASE, args.receiver_input_guard[0], int(args.receiver_input_guard[1])))
    elif args.self_test_input_lifecycle:
        from dsbridge.diagnostics.receiver_lifecycle import main as lifecycle_test
        lifecycle_test(BASE, args.self_test_input_lifecycle)
    elif args.recover_receiver_input:
        from dsbridge.controllers.flydigi.apex6.recovery import recover
        recover(BASE)
    elif args.check_input_visibility:
        from dsbridge.diagnostics.visibility import main as input_visibility
        input_visibility(["--report", str(BASE / "receiver-input-visibility.json")], quiet=True)
    elif args.self_test_receiver:
        from dsbridge.diagnostics.receiver_feedback import main as receiver_test
        receiver_test(BASE)
    elif args.hidhide_status:
        from dsbridge.diagnostics.hidhide import read_config
        (BASE / "hidhide-status.json").write_text(json.dumps(read_config(), ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.receiver_motor_test:
        from dsbridge.controllers.flydigi.apex6.session import motor_test
        motor_test(BASE)
    elif args.self_test_audio:
        from dsbridge.diagnostics.audio_protection import main as test_audio
        test_audio(["--check"])
    elif args.repair_audio:
        from dsbridge.platform.windows.audio import repair_audio_defaults
        repair_audio_defaults(BASE)
    elif args.self_test_feedback:
        from dsbridge.diagnostics.feedback import main as test_feedback
        test_feedback(["--check", "--physical"])
    elif args.self_test_touchpad:
        from dsbridge.diagnostics.touchpad import main as test_touchpad
        test_touchpad(context)
    elif args.diagnose:
        xi = XInput()
        result = {"xinput": [xi.get_state(i) for i in range(4)], "viiper_exe": (BASE.assets / "vendor/viiper/viiper.exe").exists(), "ps5_output": "蓝牙/通用 USB：原生反馈转强度；已验证接收器：四马达转换"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        (BASE / "diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        xi.close()
    elif args.motor_test is not None:
        if args.motor_test not in range(4):
            parser.error("SLOT必须是0..3")
        xi = XInput()
        try:
            if xi.set_rumble(args.motor_test, 0.22, 0) is False:
                raise RuntimeError("手柄未连接，左马达测试失败")
            time.sleep(0.25)
            if xi.set_rumble(args.motor_test, 0, 0.22) is False:
                raise RuntimeError("手柄已断开，右马达测试失败")
            time.sleep(0.25)
            if xi.set_rumble(args.motor_test, 0, 0) is False:
                raise RuntimeError("手柄已断开，无法确认停振")
        finally:
            xi.set_rumble(args.motor_test, 0, 0)
            xi.close()
        print("左右各250ms测试已完成，最后停振API成功；实际体感需要人工确认。")
    else:
        # Opening the window alone must leave the receiver usable by games.
        # A live helper owns the mutex, so a second window cannot unhide it.
        startup_recovery_warning = None
        try:
            context.recover()
        except Exception as exc:
            startup_recovery_warning = str(exc)
            if APP_DIAGNOSTICS:
                APP_DIAGNOSTICS.event("input_recovery_pending", error=str(exc))
        run_gui(smoke=args.ui_smoke or args.ui_smoke_callback or args.self_test_receiver_close,
                callback_test=args.ui_smoke_callback, receiver_close_test=args.self_test_receiver_close,
                setup=args.setup,
                initial_mode=5 if args.receiver_mode or args.self_test_receiver_close else 1, context=context,
                startup_recovery_warning=startup_recovery_warning)


def run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bounded pipe/handle workers must not initialize GUI, COM, diagnostics,
    # recovery, or the application instance before sending their stdout reply.
    workers = {"--hot-switch-worker": "hot_switch_service",
               "--controller-usage-worker": "controller_usage"}
    if any(flag in argv for flag in workers):
        if len(argv) != 2 or argv[0] not in workers:
            return 2
        import importlib
        module = importlib.import_module("dsbridge.platform.windows." + workers[argv[0]])
        return module.worker_main(argv[1]) or 0
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--data-dir", type=Path)
    args, _ = preliminary.parse_known_args(argv)
    paths = configure(RuntimePaths.discover(args.data_dir))
    diagnostics = RuntimeDiagnostics(paths.state)
    diagnostics.start()
    try:
        context = ApplicationContext(paths, diagnostics=diagnostics)
        return main(argv, context) or 0
    except Exception:
        diagnostics.exception("main", *sys.exc_info())
        # All diagnostics return an error without waiting on a modal dialog.
        interactive = not any(a.startswith(("--self-test", "--check", "--diagnose", "--detect-", "--ui-smoke", "--list", "--receiver-input-guard", "--hot-switch-input-guard", "--hot-switch-observer", "--recover-", "--install-hot-switch", "--hidhide-status", "--motor-test", "--receiver-motor-test")) for a in argv)
        if getattr(sys, "frozen", False) and interactive:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(), "八爪鱼震动桥启动错误", 0x10)
        elif sys.stderr:
            traceback.print_exc()
        return 1
    finally:
        diagnostics.close()
