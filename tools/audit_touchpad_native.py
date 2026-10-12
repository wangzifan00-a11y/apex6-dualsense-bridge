"""Verify the frozen View mapping with real human input and restored settings."""
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.audit_receiver_lifecycle import processes, read, run_hidden, snapshot, write
from tools.release_build import sha


def main():
    build = Path((ROOT / "latest-experimental-build.txt").read_text(encoding="utf-8").strip())
    build.resolve().relative_to(ROOT / "发布")
    busy = {"cyberpunk2077.exe", "hades2.exe", "viiper.exe"}
    if any(p["exe"].casefold() in busy for p in processes()):
        raise RuntimeError("请先保存并退出游戏、停止现有桥接。")
    state = ROOT / "测试记录" / "触摸板映射" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    state.mkdir(parents=True)
    output = state / "native-touchpad-audit.json"
    (ROOT / "latest-touchpad-audit.txt").write_text(str(output), encoding="utf-8")
    executable = build / "八爪鱼震动桥.exe"
    result = {"passed": False, "build": str(build), "exe_sha256": sha(executable), "state": str(state)}
    result["before"] = snapshot()
    write(output, result)
    try:
        result["exit_code"] = run_hidden([executable, "--data-dir", state, "--self-test-touchpad"],
                                         state, "touchpad", timeout=180)
        result["test"] = read(state / "touchpad-audit.json")
        result["after"] = snapshot()
        result["virtual_service_exited"] = not any(p["exe"].casefold() == "viiper.exe" for p in processes())
        journals = {}
        for name in ("receiver-input-lease.json", "receiver-session.json", "viiper-usb-session.json"):
            path = state / name
            if path.exists():
                journals[name] = read(path)
        result["restoration"] = {name: value.get("restore_pending") is False for name, value in journals.items()}
        motor = journals.get("receiver-session.json")
        result["motor_configuration_restored"] = not motor or bool(
            motor["motor_mapping_hex"] == motor["restored_mapping_hex"]
            and motor["slot"] == motor["restored_slot"] and not motor["cleanup_errors"])
        result["passed"] = bool(result["exit_code"] == 0 and result["test"]["passed"]
                                and result["before"] == result["after"] and result["virtual_service_exited"]
                                and all(result["restoration"].values()) and result["motor_configuration_restored"])
    finally:
        write(output, result)
        print(json.dumps({"passed": result["passed"], "report": str(output)}, ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise RuntimeError("实体按键验证或停止后的恢复检查未通过。")


if __name__ == "__main__":
    main()
