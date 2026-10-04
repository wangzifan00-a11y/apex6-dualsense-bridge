"""Check game-process visibility using an intentionally unallowed helper image."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from dsbridge.runtime.paths import as_paths


def verify_isolation(base):
    paths = as_paths(base)
    if not getattr(sys, "frozen", False):
        return  # Source diagnostics retain their explicit manual visibility check.
    from dsbridge.diagnostics.hidhide import read_config
    config = read_config()
    if not config["active"] or config["inverse"]:
        raise RuntimeError("接收器自动输入隔离未开启，转换已取消并尝试恢复普通输入")
    helper = paths.assets / "接收器输入检查.exe"
    if not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest() != hashlib.sha256(Path(sys.executable).read_bytes()).digest():
        raise RuntimeError("缺少完整的接收器输入检查程序，请保留整个发布目录")
    if str(helper).casefold() in {str(path).casefold() for path in config["allowed_apps"]}:
        raise RuntimeError("“接收器输入检查.exe”须保持在 HidHide 允许列表之外，才能检查游戏输入隔离")
    check = subprocess.run(paths.command("--check-input-visibility", executable=helper), timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
    if check.returncode:
        raise RuntimeError("接收器输入检查失败，请查看日志")
    report = json.loads((paths.state / "receiver-input-visibility.json").read_text(encoding="utf-8"))
    if any(row["connected"] for rows in report["libraries"].values() for row in rows):
        raise RuntimeError("接收器输入隔离未生效，转换已取消并尝试恢复普通输入")
