"""Real receiver recovery audit of the published binary, using zero gain only.

Each case has fresh writable state outside the immutable release. An idle GUI
may remain open; existing conversion owners and running games block the audit.
No process is killed by this runner, and it never deletes files.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import winreg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".localdeps"))

from dsbridge.controllers.flydigi.apex6.recovery import Native, RUN_KEY, RUN_NAME
from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim
from dsbridge.diagnostics.hidhide import read_config
from dsbridge.platform.windows.audio import WindowsAudio

CASES = ("normal", "start-failure", "runtime-failure", "crash", "window")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def processes():
    class Entry(C.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                    ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                    ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                    ("exe", W.WCHAR * 260)]
    api = C.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    api.CreateToolhelp32Snapshot.restype = W.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(api, name).argtypes = [W.HANDLE, C.POINTER(Entry)]
        getattr(api, name).restype = W.BOOL
    api.CloseHandle.argtypes = [W.HANDLE]
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    require(snapshot != C.c_void_p(-1).value, "无法读取运行进程")
    rows, entry = [], Entry()
    entry.size = C.sizeof(entry)
    try:
        more = api.Process32FirstW(snapshot, C.byref(entry))
        while more:
            rows.append({"pid": entry.pid, "exe": entry.exe})
            more = api.Process32NextW(snapshot, C.byref(entry))
        require(C.get_last_error() == 18, "进程枚举未完整结束")
    finally:
        api.CloseHandle(snapshot)
    return rows


def logon_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except FileNotFoundError:
        return None


def snapshot():
    with WindowsAudio() as audio:
        defaults = audio.snapshot()["defaults"]
    return {"hidhide": read_config(), "audio_defaults": defaults,
            "temporary_logon_entry": logon_entry()}


def verify_assets(build, manifest):
    actual = {p.relative_to(build).as_posix() for p in build.rglob("*") if p.is_file()}
    require(actual == set(manifest["files"]) | {"release-manifest.json"}, "发布目录文件列表改变")
    for name, expected in manifest["files"].items():
        target = (build / name).resolve()
        target.relative_to(build)
        with target.open("rb") as stream:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == expected,
                    "发布文件校验失败：" + name)


def run_hidden(command, state, label, timeout=220):
    # STARTF_USESHOWWINDOW also hides windowed bootloaders, unlike NO_WINDOW alone.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    with (state / (label + ".stdout.txt")).open("wb") as out, \
         (state / (label + ".stderr.txt")).open("wb") as err:
        process = subprocess.Popen(list(map(str, command)), cwd=state,
                                   stdout=out, stderr=err, startupinfo=startup,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({"case": state.name, "stage": label, "pid": process.pid},
                         ensure_ascii=False), flush=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"测试进程 {process.pid} 尚未退出；保留进程和记录，需检查后再继续")


def run_case(build, case, output):
    state = output / case
    state.mkdir()
    report_path = state / "verified-lifecycle.json"
    result = {"case": case, "build": str(build), "passed": False,
              "nonzero_test_haptics": False, "state": str(state)}
    write(report_path, result)
    try:
        busy = [p for p in processes() if p["exe"].casefold() in
                {"cyberpunk2077.exe", "hades2.exe", "streetfighter6.exe", "riftapart.exe", "viiper.exe"}]
        require(not busy, "请先关闭游戏或正在运行的转换：" + str(busy))
        native = Native()
        owner = native.claim()
        try:
            waveform = ReceiverClaim()
            waveform.close()
        finally:
            native.release(owner)
        before = snapshot()
        result["before"] = before
        require(before["temporary_logon_entry"] is None, "存在待恢复的临时登录项，未开始测试")
        write(report_path, result)
        command = [build / "八爪鱼震动桥.exe", "--data-dir", state]
        command += (["--self-test-receiver-close"] if case == "window" else
                    ["--self-test-input-lifecycle", case])
        result["exit_code"] = run_hidden(command, state, "lifecycle")
        test_file = ("receiver-window-lifecycle.json" if case == "window" else
                     f"input-lifecycle-{case}.json")
        test = read(state / test_file)
        result["test"] = test
        require(result["exit_code"] == (73 if case == "crash" else 0), "打包程序测试失败")
        if case == "crash":
            require(test["phase"] == "running", "未在转换运行时触发模拟异常退出")
        else:
            require(test.get("passed"), "测试程序报告未通过")
        lease = read(state / "receiver-input-lease.json")
        guard_path = state / ("input-guard-" + lease["token"] + ".json")
        deadline = time.monotonic() + 60
        while True:
            guard = read(guard_path)
            if guard.get("phase") == "finished":
                break
            require(time.monotonic() < deadline, "输入恢复守护进程尚未完成")
            time.sleep(.2)
        guards = [read(p) for p in state.glob("input-guard-*.json")]
        require(all(g.get("restored") and g.get("phase") == "finished" for g in guards),
                "部分转换会话尚未恢复输入")
        require(not any(value for g in guards for key, value in g.items()
                        if key.endswith(("error", "warning"))), "恢复记录中存在错误或警告")
        result["guards"] = guards
        if case == "crash":
            require(guard.get("reason") == "parent_exit", "未验证到异常退出恢复路径")
        running = processes()
        require(not any(p["exe"].casefold() == "viiper.exe" for p in running), "虚拟设备服务未退出")
        command = [build / "接收器输入检查.exe", "--data-dir", state, "--check-input-visibility"]
        require(run_hidden(command, state, "visibility", 15) == 0, "独立输入检查失败")
        visibility = read(state / "receiver-input-visibility.json")
        counts = {name: sum(row["connected"] for row in rows)
                  for name, rows in visibility["libraries"].items()}
        require(len(counts) == 2 and all(value == 1 for value in counts.values()),
                "两套 XInput 接口未同时恢复唯一实体手柄")
        result["ordinary_input_counts"] = counts
        for filename in ("receiver-input-lease.json", "receiver-session.json", "viiper-usb-session.json"):
            journal_path = state / filename
            if filename == "viiper-usb-session.json" and case == "start-failure":
                require(not journal_path.exists(), "启动失败用例意外创建虚拟 USB")
                continue
            journal = read(journal_path)
            require(journal.get("restore_pending") is False, filename + " 恢复尚未完成")
            result[filename] = journal
        motor = result["receiver-session.json"]
        require(motor["motor_mapping_hex"] == motor["restored_mapping_hex"] and
                motor["slot"] == motor["restored_slot"] and not motor["cleanup_errors"],
                "马达配置回读与开始前不一致")
        status = test.get("status_after") or test.get("running_status")
        if status:
            require(not any(status["receiver"]["motor_nonzero_writes"]), "出现非零马达输出")
            result["motor_nonzero_writes"] = status["receiver"]["motor_nonzero_writes"]
        after = snapshot()
        result["after"] = after
        require(after == before, "输入隔离或默认音频设置未恢复到测试前")
        result["passed"] = True
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        write(report_path, result)
        print(json.dumps({"case": case, "passed": result["passed"], "report": str(report_path),
                          "error": result.get("error")}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--build", type=Path)
    args = parser.parse_args()
    build = (args.build or Path((ROOT / "latest-build.txt").read_text(encoding="utf-8").strip())).resolve()
    build.relative_to((ROOT / "发布").resolve())
    manifest = read(build / "release-manifest.json")
    verify_assets(build, manifest)
    output = ROOT / "测试记录" / "模块化接收器实机" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    report_path = output / "receiver-lifecycle-audit.json"
    audit = {"passed": False, "build": str(build), "version": manifest["version"], "cases": []}
    (ROOT / "latest-receiver-lifecycle-audit.txt").write_text(str(report_path), encoding="utf-8")
    try:
        # New builds need permission to keep reading their own hidden input.
        # The independent visibility helper deliberately stays outside this list.
        from dsbridge.platform.windows.hidhide import HidHideControl
        with HidHideControl() as driver:
            before_allow = driver.snapshot()
            driver.allow(build / "八爪鱼震动桥.exe")
            after_allow = driver.snapshot()
        require(all(before_allow[k] == after_allow[k] for k in ("hidden_devices", "active", "inverse")),
                "允许新构建时意外改变输入隐藏配置")
        require(not any(p.casefold().endswith("接收器输入检查.exe") for p in after_allow["allowed_nt"]),
                "独立输入检查程序不能在允许列表中")
        audit["allow_new_build"] = {"before": before_allow, "after": after_allow}
        for case in (CASES if args.case == "all" else (args.case,)):
            audit["cases"].append(run_case(build, case, output))
        verify_assets(build, manifest)
        audit.update(passed=True, resources_unchanged=True)
    finally:
        write(report_path, audit)
        print(json.dumps({"passed": audit["passed"], "report": str(report_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
