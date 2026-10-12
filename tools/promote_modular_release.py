"""Promote only the checked portable build and preserve existing input rules."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.release_build import sha, verify_clean
from dsbridge.platform.windows.hidhide import HidHideControl


def main():
    audit_path = Path((ROOT / "latest-portable-audit.txt").read_text(encoding="utf-8").strip())
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    release = Path(audit["build"])
    release.resolve().relative_to(ROOT / "发布")
    assert audit["passed"] and audit["resources_unchanged"] and audit["frozen_plugin_loaded"]
    executable = release / "八爪鱼震动桥.exe"
    assert sha(executable) == audit["exe_sha256"]
    assert sha(audit["zip"]) == audit["zip_sha256"]
    native_path = Path((ROOT / "latest-ps5-native-audit.txt").read_text(encoding="utf-8").strip())
    native = json.loads(native_path.read_text(encoding="utf-8"))
    assert native["passed"] and native["build"] == str(release) and native["exe_sha256"] == audit["exe_sha256"]
    lifecycle_path = Path((ROOT / "latest-receiver-lifecycle-audit.txt").read_text(encoding="utf-8").strip())
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    assert lifecycle["passed"] and lifecycle["build"] == str(release) and lifecycle["resources_unchanged"]
    assert {case["case"] for case in lifecycle["cases"] if case["passed"]} == {"normal", "start-failure", "runtime-failure", "crash", "window"}
    hot_path = Path((ROOT / "latest-hot-switch-audit.txt").read_text(encoding="utf-8").strip())
    hot = json.loads(hot_path.read_text(encoding="utf-8"))
    assert hot["passed"] and hot["build"] == str(release) and hot["exe_sha256"] == audit["exe_sha256"]
    verify_clean(release)
    with HidHideControl() as driver:
        before = driver.snapshot()
        driver.allow(executable)
        after = driver.snapshot()
    assert all(before[key] == after[key] for key in ("hidden_devices", "active", "inverse"))
    assert set(before["allowed_nt"]).issubset(after["allowed_nt"])
    assert not any(value.casefold().endswith("接收器输入检查.exe".casefold()) for value in after["allowed_nt"])
    report = {"passed": True, "before": before, "after": after, "build": str(release),
              "exe_sha256": audit["exe_sha256"], "portable_audit": str(audit_path), "native_audit": str(native_path),
              "lifecycle_audit": str(lifecycle_path), "hot_switch_audit": str(hot_path)}
    (audit_path.parent / "promotion.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "latest-build.txt").write_text(str(release), encoding="utf-8")
    print(str(release))


if __name__ == "__main__":
    main()
