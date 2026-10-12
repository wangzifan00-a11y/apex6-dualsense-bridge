"""Read-only device/process capture for a human-operated disconnect test.

Only writes evidence under the specified workspace output directory. It never
starts/stops processes, changes device configuration, or sends motor commands.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from audit_receiver_lifecycle import ROOT, processes, read, snapshot, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("running", "disconnected"))
    parser.add_argument("--case", choices=("poweroff", "unplug"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to(ROOT / "测试记录")
    output.mkdir(parents=True, exist_ok=True)
    session = read(args.state / "feedback-session.json")
    lease = read(args.state / "receiver-input-lease.json")
    guard = read(args.state / ("input-guard-" + lease["token"] + ".json"))
    running = processes()
    result = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "case": args.case,
        "stage": args.stage,
        "session": session,
        "lease": lease,
        "guard": guard,
        "usb": read(args.state / "viiper-usb-session.json"),
        "motor": read(args.state / "receiver-session.json"),
        "snapshot": snapshot(),
        "processes": [p for p in running if p["exe"].casefold() in
                      {"八爪鱼震动桥.exe", "接收器输入检查.exe", "viiper.exe"}],
        "passed": False,
    }
    pids = {p["pid"] for p in running}
    checks = {
        "gui_alive": session["process_id"] in pids,
        "no_nonzero_motor_output": not any(session["ps5"]["receiver"]["motor_nonzero_writes"]),
    }
    if args.stage == "running":
        checks.update(running=session["phase"] == "running",
                      guard_alive=guard["guard_pid"] in pids,
                      virtual_device_attached=result["usb"]["restore_pending"] is True)
    else:
        before = read(output / (args.case + "-running.json"))
        baseline = read(output / "before.json")
        helpers = {p["pid"] for p in before["processes"]
                   if p["exe"].casefold() == "viiper.exe"}
        helpers.add(before["guard"]["guard_pid"])
        checks.update(
            same_session=session["session_id"] == before["session"]["session_id"],
            disconnected=session["phase"] == "disconnected",
            helpers_exited=not (helpers & pids),
            guard_restored=guard.get("phase") == "finished" and guard.get("restored") is True,
            input_restored=lease["restore_pending"] is False,
            virtual_device_removed=result["usb"]["restore_pending"] is False,
            audio_and_hiding_restored=result["snapshot"] == baseline["snapshot"],
        )
    result["checks"] = checks
    result["passed"] = all(checks.values())
    target = output / (args.case + "-" + args.stage + ".json")
    write(target, result)
    print({"passed": result["passed"], "checks": checks, "report": str(target)})
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
