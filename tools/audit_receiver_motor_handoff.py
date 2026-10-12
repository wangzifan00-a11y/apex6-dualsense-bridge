"""Explicit physical regression: frozen GUI detection immediately before motor tests."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.audit_receiver_lifecycle import processes, read, require, run_hidden, snapshot, write
from tools.release_build import sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--physical", action="store_true", required=True)
    args = parser.parse_args()
    build = Path((ROOT / "latest-experimental-build.txt").read_text(encoding="utf-8").strip())
    executable = build / "八爪鱼震动桥.exe"
    require(not any(p["exe"].casefold() in {"cyberpunk2077.exe", "hades2.exe", "viiper.exe"}
                    for p in processes()), "请先关闭游戏并停止转换")
    state = ROOT / "测试记录/四马达检测衔接" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    state.mkdir(parents=True)
    before = snapshot()
    report = {"build": str(build), "exe_sha256": sha(executable), "passed": False,
              "physical_rumble": True, "cases": [], "before": before}
    output = state / "motor-handoff-audit.json"
    try:
        for attempt in range(1, 3):
            case = state / str(attempt)
            case.mkdir()
            command = [executable, "--data-dir", case]
            require(run_hidden(command + ["--ui-smoke", "--receiver-mode"], case, "gui-detection") == 0,
                    "打包版界面检测失败")
            gui = read(case / "gui-smoke.json")
            require(gui["connection_check_finished"] and gui["connection"]["kind"] == "receiver"
                    and gui["selected_profile"] == 5, "未识别到接收器四马达连接")
            require(run_hidden(command + ["--receiver-motor-test"], case, "four-motors") == 0,
                    "检测后四马达测试仍失败")
            motor = read(case / "receiver-motor-test.json")
            journal = read(case / "receiver-session.json")
            require([p["name"] for p in motor["positions"]] == ["左握把", "右握把", "左扳机", "右扳机"]
                    and all(p["waveform_packets"] > 0 for p in motor["positions"]), "四路测试没有完整执行")
            require(motor["restored"] and not motor.get("error") and not motor["cleanup_errors"], "测试停振或恢复失败")
            require(not journal["restore_pending"] and journal["motor_mapping_hex"] == journal["restored_mapping_hex"]
                    and journal["slot"] == journal["restored_slot"], "恢复后的配置与测试前不一致")
            report["cases"].append({"attempt": attempt, "gui": gui, "motor": motor, "journal": journal})
            print(json.dumps({"attempt": attempt, "passed": True, "positions": motor["positions"],
                              "restored": motor["restored"]}, ensure_ascii=False), flush=True)
        report["after"] = snapshot()
        require(before == report["after"], "输入隔离或默认音频设置发生变化")
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        write(output, report)
        (ROOT / "latest-motor-handoff-audit.txt").write_text(str(output), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
