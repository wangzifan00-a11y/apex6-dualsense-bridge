"""Bind native DS audio protection evidence to the exact new executable."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.release_build import sha
from tools.audit_receiver_lifecycle import run_hidden, snapshot, processes


def main():
    build = Path((ROOT / "latest-experimental-build.txt").read_text(encoding="utf-8").strip())
    build.resolve().relative_to(ROOT / "发布")
    state = ROOT / "测试记录" / "PS5原生检查" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    state.mkdir(parents=True)
    executable = build / "八爪鱼震动桥.exe"
    before = snapshot()
    assert not any(p["exe"].casefold() == "viiper.exe" for p in processes())
    code = run_hidden([executable, "--data-dir", state, "--self-test-audio"], state, "audio")
    result = json.loads((state / "audio-protection-integration.json").read_text(encoding="utf-8"))
    usb = json.loads((state / "viiper-usb-session.json").read_text(encoding="utf-8"))
    after = snapshot()
    passed = (code == 0 and result["result"] == "passed" and not result["physical_rumble"]
              and not result["microphone_recording"] and not usb["restore_pending"] and before == after
              and not any(p["exe"].casefold() == "viiper.exe" for p in processes()))
    report = {"passed": passed, "build": str(build), "exe_sha256": sha(executable), "before": before,
              "after": after, "audio": result, "usb": usb}
    output = state / "native-audit.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "latest-ps5-native-audit.txt").write_text(str(output), encoding="utf-8")
    print(json.dumps({"passed": passed, "report": str(output)}, ensure_ascii=False))
    assert passed, "PS5 native audio check failed"


if __name__ == "__main__":
    main()
