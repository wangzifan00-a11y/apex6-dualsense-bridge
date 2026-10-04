"""Dependency orchestration; no direct native calls or UI dependencies."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid
from dsbridge.runtime.paths import as_paths
from dsbridge.dependencies.catalog import (Package, Dependency, PACKAGES, LABELS, SETUP_STATE,
                                            verify_package, installer_arguments, write_report)
from dsbridge.platform.windows.install import run_installer
from dsbridge.dependencies.windows_probe import WindowsProbe

class DependencyManager:
    def __init__(self, base: Path, *, probe=None, launcher=run_installer, verifier=verify_package, packages=PACKAGES):
        self.paths = as_paths(base)
        self.base = self.paths.state
        self.packages = packages
        self.probe = probe or WindowsProbe(self.paths.assets)
        self.launcher, self.verifier = launcher, verifier
        self._install_lock = threading.Lock()

    def _pending_restart(self, key):
        try:
            data = json.loads((self.base / SETUP_STATE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        # Do not reuse this PC's reboot state when the folder is copied elsewhere.
        identity = self.probe.identity()
        return bool(identity and data.get("identity") == identity and data.get("restart", {}).get(key))

    def _check_one(self, package):
        key = package.key
        def result(state, detail, version=""):
            return Dependency(key, package.name, state, detail, version)
        if not self.probe.supported():
            return result("unsupported", "此安装包适用于 Windows 10/11 的 Intel/AMD 64 位电脑。")
        if key == "hidhide" and self.probe.service("HidGuardian"):
            return result("conflict", "检测到 HidGuardian。请通过它的官方卸载程序处理冲突并重启，再检查 HidHide。")
        if self._pending_restart(key):
            return result("restart", "安装程序要求重启。请保存工作并重启 Windows，再打开本程序。")
        if key not in ("usbip", "hidhide"):
            return result("unsupported", "此版本只管理 PS5 桥接所需依赖。")
        service = self.probe.service("usbip2_ude" if key == "usbip" else "HidHide")
        cli = self.probe.cli_exists(key)
        if not service and not cli and not self.probe.product_exists(key):
            return result("missing", "点击“安装缺失依赖”安装随程序提供的官方安装包。")
        if not service or not cli:
            return result("unavailable", "发现已有安装，但驱动或工具不完整。先重启；仍异常时请用原安装程序修复。")
        if key == "usbip":
            code, version = self.probe.usbip_command("--version")
            if code:
                return result("unavailable", "USBip 版本查询失败：" + version[:400])
            if version != package.version:
                return result("mismatch", "本程序要求 USBip 0.9.7.7；当前为 " + version[:80] + "。请在 Windows 已安装的应用中处理旧版本后重试。", version)
            code, output = self.probe.usbip_command("port")
            if code:
                return result("unavailable", "已安装，但无法访问 USBip 驱动。请先重启；仍失败则修复驱动。" + output[:400], version)
            return result("ready", "版本和驱动访问检查通过。", version)
        try:
            self.probe.hidhide_ready()
        except OSError as exc:
            return result("unavailable", "已安装，但无法访问 HidHide。先关闭其配置工具再检查；仍失败请重启。" + str(exc))
        return result("ready", "驱动接口检查通过。接收器转换时自动管理输入隐藏；蓝牙沿用原配置方式。")

    def check(self):
        rows = []
        for package in self.packages:
            try:
                row = self._check_one(package)
            except Exception as exc:
                row = Dependency(package.key, package.name, "error", "无法确认安装状态：" + str(exc))
            row.package_ok, row.package_detail = self.verifier(self.paths.assets, package)
            rows.append(row)
        return rows

    def require(self, mode):
        from dsbridge.core.modes import PROFILES
        if mode not in PROFILES:
            raise ValueError("此版本仅支持 PS5 DualSense 桥接模式")
        self.require_keys(PROFILES[mode].dependencies)

    def require_keys(self, keys):
        rows = {row.key: row for row in self.check()}
        unknown = set(keys) - rows.keys()
        if unknown:
            raise RuntimeError("未注册的依赖：" + ", ".join(sorted(unknown)))
        problems = [rows[key] for key in keys if rows[key].state != "ready"]
        if problems:
            raise RuntimeError("DS 转换依赖尚未就绪。请打开“依赖检查与安装”：\n" +
                               "\n".join(row.name + "：" + LABELS[row.state] + "。" + row.detail for row in problems))

    def install_missing(self, notify=lambda message: None):
        if not self._install_lock.acquire(blocking=False):
            raise RuntimeError("安装已在进行中，请等待完成。")
        results = []
        try:
            # Recheck immediately before each launch. Never replace an existing driver.
            for package in self.packages:
                row = next(row for row in self.check() if row.key == package.key)
                if not row.can_install:
                    results.append({"key": package.key, "outcome": "skipped", "state": row.state,
                                    "message": row.detail if row.state != "missing" else row.package_detail})
                    continue
                log_dir = self.base / "logs"
                log_dir.mkdir(exist_ok=True)
                log_path = log_dir / ("install-" + package.key + "-" + uuid.uuid4().hex + ".log")
                # Hash again at the execution boundary, including retries from the UI.
                valid, detail = self.verifier(self.paths.assets, package)
                if not valid:
                    raise RuntimeError(detail)
                notify("正在安装 " + package.name + "；请确认 Windows 管理员权限提示。")
                try:
                    code = self.launcher(self.paths.assets / "installers" / package.filename, installer_arguments(package, log_path))
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 1223:
                        results.append({"key": package.key, "outcome": "cancelled", "message": "已取消管理员授权，可稍后重新安装。"})
                        break
                    raise
                item = {"key": package.key, "exit_code": code, "log": str(log_path)}
                if code in (0, 3010, 1641):
                    pending = code in (3010, 1641)
                    state_path = self.base / SETUP_STATE
                    identity = self.probe.identity()
                    state = {"identity": identity, "restart": {}}
                    if state_path.exists():
                        old = json.loads(state_path.read_text(encoding="utf-8"))
                        if old.get("identity") == identity:
                            state = old
                    state["restart"][package.key] = pending
                    state["updated_at"] = datetime.now(timezone.utc).isoformat()
                    write_report(state_path, state)
                    checked = next(row for row in self.check() if row.key == package.key)
                    item.update(outcome="restart" if pending else "installed" if checked.state == "ready" else "unavailable",
                                state=checked.state, message="请保存工作后手动重启电脑。" if pending else checked.detail)
                elif code in (1223, 1602):
                    item.update(outcome="cancelled", message="安装已取消，可稍后重试。")
                else:
                    item.update(outcome="failed", message="安装失败，返回码 " + str(code) + "。详情见日志：" + str(log_path))
                results.append(item)
                notify(package.name + "：" + item["message"])
                if item["outcome"] in ("failed", "cancelled"):
                    break
            report = {"finished_at": datetime.now(timezone.utc).isoformat(), "results": results,
                      "dependencies": [row.to_dict() for row in self.check()]}
            write_report(self.base / "dependency-install-result.json", report)
            return report
        finally:
            self._install_lock.release()
