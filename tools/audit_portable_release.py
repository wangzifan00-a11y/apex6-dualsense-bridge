"""Verify a relocated ZIP with fresh data and no Python/PATH dependency."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".localdeps"))
from tools.release_build import verify_clean, sha, EXCLUDED


def main():
    release = Path((ROOT / "latest-experimental-build.txt").read_text(encoding="utf-8").strip())
    archive = Path((ROOT / "latest-portable-archive.txt").read_text(encoding="utf-8").strip())
    release.resolve().relative_to(ROOT / "发布")
    expected_zip = archive.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    assert sha(archive) == expected_zip
    test = ROOT / "测试记录" / ("便携版 中文 空目录 " + uuid.uuid4().hex)
    test.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        for name in package.namelist():
            assert (test / name).resolve().is_relative_to(test.resolve())
        assert package.testzip() is None
        package.extractall(test)
    copied = test / release.name
    manifest = json.loads((copied / "release-manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert sha(copied / name) == digest, name
    verify_clean(copied)
    executable = copied / "八爪鱼震动桥.exe"
    from PyInstaller.archive.readers import CArchiveReader
    modules = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz").toc
    assert not any(m == name or m.startswith(name + ".") for m in modules for name in EXCLUDED)
    for name in ("dsbridge.core.engine", "dsbridge.controllers.registry", "dsbridge.controllers.flydigi.apex6.output",
                 "dsbridge.virtual.dualsense.codec", "dsbridge.platform.windows.connection", "dsbridge.dependencies.manager"):
        assert name in modules, name
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    env["PATH"] = str(Path(env.get("SystemRoot", r"C:\Windows")) / "System32")
    env["LOCALAPPDATA"] = str(test / "空白用户目录")
    checks = []
    def run(*args):
        result = subprocess.run([str(executable), *args], cwd=test, env=env, timeout=55,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, (args, result.returncode)
        checks.append(list(args))
    # Default discovery writes to the fresh LocalAppData, not the executable folder.
    run("--check-dependencies")
    state = test / "空白用户目录" / "DSFeedbackBridge"
    deps = json.loads((state / "dependencies.json").read_text(encoding="utf-8"))["dependencies"]
    assert {row["key"] for row in deps} == {"usbip", "hidhide"}
    assert not any("vigem" in p.name.casefold() for p in copied.rglob("*"))
    assert all(row["package_ok"] for row in deps)
    run("--ui-smoke", "--setup")
    gui = json.loads((state / "gui-smoke.json").read_text(encoding="utf-8"))
    assert gui["dependency_check_finished"]
    assert [row["id"] for row in gui["mode_selections"]] == [1, 5]
    assert gui["connection_check_finished"] and gui["automatic_modes"]
    assert not any("PS4" in text or "DualShock" in text for text in gui["main_controls"])
    assert gui["removed_modules_loaded"] == []
    assert gui["requested_size"][0] <= 850 and gui["requested_size"][1] <= 765
    assert gui["setup_requested_size"][0] <= 780 and gui["setup_requested_size"][1] <= 750
    assert gui["gain_percent"] == 70 and gui["engine_gain"] == .7
    # Two separate frozen processes must read the same user setting.
    # This state belongs to the relocated audit, never to the user's real GUI.
    (state / "ui-settings.json").write_text(json.dumps({"version": 1, "gain_percent": 42}), encoding="utf-8")
    gain_restarts = []
    for _ in range(2):
        run("--ui-smoke")
        reopened = json.loads((state / "gui-smoke.json").read_text(encoding="utf-8"))
        assert reopened["gain_percent"] == 42 and reopened["engine_gain"] == .42
        assert reopened["gain_label"] == "转换强度 42%"
        gain_restarts.append(reopened["gain_percent"])
    run("--ui-smoke-callback")
    callback = json.loads((state / "gui-callback-smoke.json").read_text(encoding="utf-8"))
    assert callback["callback_caught"] and callback["window_alive"]
    # Verify explicit state propagation and local extension loading in the frozen runtime.
    custom = test / "扩展测试数据"
    custom.mkdir()
    (custom / "plugins").mkdir()
    shutil.copytree(ROOT / "examples/dsbridge_ext_example", custom / "plugins/dsbridge_ext_example")
    (custom / "adapters.json").write_text('["dsbridge_ext_example"]', encoding="utf-8")
    run("--data-dir", str(custom), "--list-adapters")
    adapters = json.loads((custom / "adapters-report.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in adapters} == {"xinput", "flydigi.apex6.receiver", "example.model"}
    verify_clean(copied)
    assert {str(p.relative_to(copied)).replace("\\", "/") for p in copied.rglob("*") if p.is_file()} == set(manifest["files"]) | {"release-manifest.json"}
    for name, digest in manifest["files"].items():
        assert sha(copied / name) == digest, name
    report = {"passed": True, "version": manifest["version"], "build": str(release),
              "exe_sha256": sha(release / executable.name), "zip": str(archive), "zip_sha256": expected_zip,
              "relocated_folder": str(copied), "data_folder": str(state), "checks": checks,
              "python_not_required": True, "resources_unchanged": True, "native_dependencies": deps,
              "gui": gui, "frozen_plugin_loaded": True, "gain_restarts": gain_restarts,
              "fresh_pc_installation": "covered by fake-driver tests; this run uses the current Windows drivers"}
    output = test / "portable-audit.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "latest-portable-audit.txt").write_text(str(output), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
