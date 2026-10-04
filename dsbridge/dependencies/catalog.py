"""Pinned offline driver packages and platform-neutral result types."""
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path

@dataclass(frozen=True)
class Package:
    key: str
    name: str
    version: str
    filename: str
    sha256: str
    source: str


PACKAGES = (
    Package("usbip", "USBip", "0.9.7.7", "USBip-0.9.7.7-x64.exe",
            "51620fa5f9f8be5932bc9d786deee557ce06d5407a99cab490dcfac71f185fea",
            "https://github.com/vadimgrn/usbip-win2/releases/tag/v.0.9.7.7"),
    Package("hidhide", "HidHide", "1.5.230", "HidHide_1.5.230_x64.exe",
            "f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6",
            "https://github.com/nefarius/HidHide/releases/tag/v1.5.230.0"),
)
LABELS = {
    "ready": "已就绪", "missing": "未安装", "restart": "需要重启",
    "unavailable": "驱动未就绪", "mismatch": "版本不匹配",
    "conflict": "存在冲突", "error": "检查失败", "unsupported": "系统不支持",
}
SETUP_STATE = "dependency-install-state.json"


@dataclass
class Dependency:
    key: str
    name: str
    state: str
    detail: str
    version: str = ""
    package_ok: bool = False
    package_detail: str = ""

    @property
    def can_install(self):
        return self.state == "missing" and self.package_ok

    def to_dict(self):
        return {**asdict(self), "label": LABELS[self.state], "can_install": self.can_install}


def write_report(path: Path, value):
    # Keep one explicit file; no deletion or recursive cleanup.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_package(base: Path, package: Package):
    path = base / "installers" / package.filename
    try:
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        return False, "安装包缺失或无法读取。请复制完整发布文件夹。" + str(exc)
    if digest != package.sha256:
        return False, "安装包校验失败。请重新取得完整发布文件夹。"
    return True, "已包含校验通过的离线安装包 " + package.version


def installer_arguments(package: Package, log_path: Path):
    if package.key == "usbip":
        return ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RESTARTEXITCODE=3010", "/LOG=" + str(log_path)]
    return ["/exenoui", "/qn", "/norestart", "REBOOT=ReallySuppress", "/L*V", str(log_path)]


