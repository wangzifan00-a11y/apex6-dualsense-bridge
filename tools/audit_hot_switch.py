"""Run the frozen, zero-gain normal hot-switch audit with old input handles.

This tool never installs the restart service, changes HidHide permissions,
terminates games, or claims a real game was tested. Prepare the exact build and
service first. A timed-out bridge is left running for its recovery guard.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".localdeps"))

from tools.audit_receiver_lifecycle import processes, require, snapshot, verify_assets
from dsbridge.controllers.flydigi.apex6.recovery import Native
from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim
from dsbridge.diagnostics.hot_switch import audit_passed, read, save_json as write
from dsbridge.platform.windows.hot_switch_service import HotSwitchClient
from dsbridge.runtime.paths import RuntimePaths


def run(build, output):
    state = output / "normal"
    state.mkdir()
    report_file = output / "hot-switch-audit.json"
    result = {"case": "normal", "build": str(build), "state": str(state), "passed": False,
              "game_validation": False, "zero_gain": True}
    write(report_file, result)
    child = None
    try:
        result["exe_sha256"] = hashlib.sha256((build / "八爪鱼震动桥.exe").read_bytes()).hexdigest()
        manifest = read(build / "release-manifest.json")
        verify_assets(build, manifest)
        result["version"] = manifest["version"]
        busy = [row for row in processes() if row["exe"].casefold() in
                {"cyberpunk2077.exe", "hades2.exe", "streetfighter6.exe", "riftapart.exe", "viiper.exe"}]
        require(not busy, "请先关闭游戏和正在运行的转换，再执行零强度审计")
        native = Native()
        claim = native.claim()
        try:
            waveform = ReceiverClaim()
            waveform.close()
        finally:
            native.release(claim)
        HotSwitchClient(RuntimePaths(build, state)).status()
        result["before"] = snapshot()
        before = result["before"]
        allowed = {str(Path(p).resolve()).casefold() for p in before["hidhide"]["allowed_apps"]}
        require(str((build / "八爪鱼震动桥.exe").resolve()).casefold() in allowed,
                "主程序还未在 HidHide 允许列表中；本审计不修改允许列表")
        require(str((build / "接收器输入检查.exe").resolve()).casefold() not in allowed,
                "独立观察程序必须位于 HidHide 允许列表之外")
        require(before["temporary_logon_entry"] is None, "存在尚未完成的输入恢复，未开始审计")
        write(report_file, result)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        with (state / "audit.stdout.txt").open("wb") as stdout, (state / "audit.stderr.txt").open("wb") as stderr:
            child = subprocess.Popen([str(build / "八爪鱼震动桥.exe"), "--data-dir", str(state), "--self-test-hot-switch"],
                                     cwd=state, stdout=stdout, stderr=stderr, startupinfo=startup,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            result["pid"] = child.pid
            write(report_file, result)
            print(json.dumps({"stage": "normal", "pid": child.pid, "state": str(state)}, ensure_ascii=False), flush=True)
            try:
                result["exit_code"] = child.wait(timeout=480)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"测试进程 {child.pid} 尚未退出；保留进程和记录，等待其输入恢复") from exc
        test = read(state / "hot-switch-audit.json")
        result["test"] = test
        require(result["exit_code"] == 0 and test.get("passed") is True and audit_passed(test), "打包版旧句柄审计未通过")
        result["after"] = snapshot()
        require(result["after"] == before, "HidHide、默认音频或临时恢复项未恢复到审计之前")
        live_pids = {row["pid"] for row in processes()}
        require(test.get("observer_pid") not in live_pids, "只读观察进程尚未退出")
        for path in state.glob("input-guard-*.json"):
            guard = read(path)
            require(guard.get("phase") == "finished" and guard.get("restored") is True,
                    "热切换恢复守护进程尚未完成")
            require(guard.get("guard_pid") not in live_pids, "热切换恢复守护进程尚未退出")
        transaction = read(state / "hot-switch-transaction.json")
        require(transaction.get("restore_pending") is False and transaction.get("restart_needed") is False,
                "热切换事务仍有待恢复项")
        verify_assets(build, manifest)
        result.update(passed=True, resources_unchanged=True)
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        if child and child.poll() is None:
            result["process_still_running"] = True
        write(report_file, result)
        if result["passed"]:
            (ROOT / "latest-hot-switch-audit.txt").write_text(str(report_file), encoding="utf-8")
        print(json.dumps({"passed": result["passed"], "report": str(report_file), "error": result.get("error")},
                         ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--case", choices=("normal",), default="normal")
    args = parser.parse_args()
    build = (args.build or Path((ROOT / "latest-experimental-build.txt").read_text(encoding="utf-8").strip())).resolve()
    build.relative_to((ROOT / "发布").resolve())
    output = ROOT / "测试记录" / "自动热切换" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    run(build, output)


if __name__ == "__main__":
    main()
