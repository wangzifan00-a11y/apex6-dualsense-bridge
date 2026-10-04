"""Create a clean, relocatable Windows folder and ZIP. Never remove old builds."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

from dsbridge import __version__ as VERSION
APP = "八爪鱼震动桥"
ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = ("audio_haptics", "xbox_ds_backend", "probe_xbox_ds", "dsbridge.legacy", "dsbridge.virtual.dualshock4")
RUNTIME_FILES = {"controller-identity.json", "audio-defaults.json", "receiver-hidhide-session.json",
                 "receiver-session.json", "receiver-input-lease.json", "feedback-session.json",
                 "hidhide-session.json", "dependency-install-state.json", "viiper-usb-session.json"}


def sha(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_clean(directory):
    leaks = [str(p.relative_to(directory)) for p in directory.rglob("*")
             if p.is_file() and (p.name in RUNTIME_FILES or "logs" in p.relative_to(directory).parts
                                or p.suffix in (".lnk", ".download", ".download2"))]
    if leaks:
        raise RuntimeError("发布包混入运行状态或本机文件：" + ", ".join(leaks))


def assemble_assets(target):
    from dsbridge.dependencies.catalog import PACKAGES, verify_package
    (target / "vendor").mkdir()
    shutil.copytree(ROOT / "vendor" / "viiper", target / "vendor" / "viiper")
    (target / "installers").mkdir()
    for package in PACKAGES:
        valid, message = verify_package(ROOT, package)
        if not valid:
            raise RuntimeError(package.name + ": " + message)
        shutil.copy2(ROOT / "installers" / package.filename, target / "installers" / package.filename)
    for name in ("使用说明.txt", "检查并安装依赖.cmd"):
        shutil.copy2(ROOT / name, target / name)
    shutil.copytree(ROOT / "docs", target / "docs")
    shutil.copytree(ROOT / "examples", target / "examples", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / "viiper-config.json").write_text(json.dumps({"host": "127.0.0.1", "port": 3242, "haptics_gain": 1.0}, indent=2), encoding="utf-8")
    for name, arguments in (("启动蓝牙DS桥接.cmd", ""), ("启动2.4G四马达DS桥接.cmd", "--receiver-mode")):
        (target / name).write_text('@echo off\nchcp 65001 >nul\nstart "" "%~dp0' + APP + '.exe" ' + arguments + '\n', encoding="utf-8-sig")
    # Keep a byte-identical helper outside HidHide's allow-list.
    shutil.copy2(target / (APP + ".exe"), target / "接收器输入检查.exe")


def manifest(target):
    from dsbridge.dependencies.catalog import PACKAGES
    from dataclasses import asdict
    verify_clean(target)
    data = {"version": VERSION, "built_at": datetime.now(timezone.utc).isoformat(),
            "platform": "Windows 10/11 x64", "python": sys.version.split()[0],
            "entry": APP + ".exe", "state_directory": "%LOCALAPPDATA%\\DSFeedbackBridge",
            "adapter_api": 1, "drivers": [asdict(p) for p in PACKAGES],
            "excluded_modules": EXCLUDED,
            "files": {str(p.relative_to(target)).replace("\\", "/"): sha(p)
                      for p in sorted(target.rglob("*")) if p.is_file()}}
    (target / "release-manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def make_zip(target):
    archive = target.parent / (APP + "-" + VERSION + "-Windows-x64.zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                output.write(path, str(path.relative_to(target.parent)))
    (archive.with_suffix(".zip.sha256")).write_text(sha(archive) + "  " + archive.name + "\n", encoding="utf-8")
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a new DS-only portable release")
    parser.add_argument("--experimental", action="store_true", help="兼容旧构建参数；构建均先生成待验证版本")
    args = parser.parse_args(argv)
    if os.name != "nt" or sys.maxsize < 2**32:
        raise RuntimeError("请在 Windows x64 Python 下构建")
    sys.path.insert(0, str(ROOT / ".localdeps"))
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".pyinstaller-cache"))
    from dsbridge.dependencies.catalog import PACKAGES, verify_package
    for package in PACKAGES:
        valid, detail = verify_package(ROOT, package)
        if not valid:
            raise RuntimeError(package.name + " " + detail)
    import PyInstaller.__main__
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    work = ROOT / "build" / stamp
    work.mkdir(parents=True)
    options = [str(ROOT / "app.py"), "--onedir", "--windowed", "--name", APP,
               "--distpath", str(ROOT / "发布" / stamp), "--workpath", str(work / "objects"),
               "--specpath", str(work), "--paths", str(ROOT / ".localdeps"),
               "--collect-all", "soundcard", "--collect-all", "numpy", "--hidden-import", "_cffi_backend"]
    for name in EXCLUDED:
        options += ["--exclude-module", name]
    PyInstaller.__main__.run(options)
    target = ROOT / "发布" / stamp / APP
    assemble_assets(target)
    manifest(target)
    archive = make_zip(target)
    (ROOT / "latest-experimental-build.txt").write_text(str(target), encoding="utf-8")
    (ROOT / "latest-portable-archive.txt").write_text(str(archive), encoding="utf-8")
    print(json.dumps({"build": str(target), "zip": str(archive), "version": VERSION}, ensure_ascii=False), flush=True)
    return 0
