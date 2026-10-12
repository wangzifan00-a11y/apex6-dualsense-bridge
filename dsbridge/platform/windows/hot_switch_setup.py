"""One explicit first-use elevation for the restricted restart service.

Normal startup only installs a proved missing SCM service. The separately
invoked installation command may request a strictly validated native upgrade.
Cancellation or failure is never retried automatically in this
process; another explicit app launch can try again. No service/device mutation
is performed by importing this module or validating the bundled artifact.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import threading

from dsbridge.platform.windows.hot_switch_service import (
    API_VERSION, HELPER_VERSION, HotSwitchClient, HotSwitchError,
    _status_reply, current_user_sid, helper_artifact,
)
from dsbridge.platform.windows.install import run_installer
from dsbridge.runtime.paths import as_paths

_gate = threading.Lock()
_attempted = set()
_SHA = re.compile(r"[0-9a-fA-F]{64}")


def _read_manifest(path):
    try:
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Manifest is too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Manifest is not an object")
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise HotSwitchError("helper_unverified", "自动热切换组件缺少有效校验记录，请重新取得完整程序包。") from exc


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _expected_digest(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise HotSwitchError("helper_unverified", "自动热切换组件校验记录无效。")
    return value.casefold()


def verify_helper_artifact(paths):
    """Validate the exact helper against its package, never a bare .sha256 file."""
    paths = as_paths(paths)
    candidate = helper_artifact(paths)
    allowed = {paths.assets / "native/hotswitch/DSBridgeHotSwitch.exe",
               paths.assets / "native/hotswitch/bin/DSBridgeHotSwitch.exe"}
    if candidate not in allowed:
        raise HotSwitchError("helper_unverified", "自动热切换组件不在程序包的固定位置。")
    try:
        if not candidate.resolve().is_relative_to(paths.assets):
            raise HotSwitchError("helper_unverified", "自动热切换组件路径超出程序包。")
        # Reject links/junctions within the bundle. The elevation target must be
        # the exact reviewed artifact, not another file reached through a link.
        for item in (candidate, *candidate.parents):
            details = item.lstat()
            if (stat.S_ISLNK(details.st_mode) or
                    getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise HotSwitchError("helper_unverified", "自动热切换组件不能通过链接或目录联接运行。")
            if item == paths.assets:
                break
        if not 1024 <= candidate.stat().st_size <= 16 * 1024 * 1024:
            raise HotSwitchError("helper_unverified", "自动热切换组件文件大小无效。")
        relative = candidate.relative_to(paths.assets).as_posix()
        if candidate.parent.name == "bin":
            manifest = _read_manifest(candidate.parent / "build-manifest.json")
            if (manifest.get("version") != HELPER_VERSION or type(manifest.get("api_version")) is not int
                    or manifest["api_version"] != API_VERSION):
                raise HotSwitchError("helper_unverified", "开发版热切换组件版本不匹配。")
            sources = manifest.get("sources")
            source_root = paths.assets / "native/hotswitch"
            actual = {item.relative_to(paths.assets).as_posix(): _hash(item)
                      for item in sorted(source_root.glob("*.cs")) if item.is_file()}
            if not actual or sources != actual:
                raise HotSwitchError("helper_unverified", "热切换组件源码已改变，请先重新构建组件。")
            expected = _expected_digest(manifest.get("sha256"))
        else:
            manifest = _read_manifest(paths.assets / "release-manifest.json")
            files = manifest.get("files")
            if not isinstance(files, dict) or relative not in files:
                raise HotSwitchError("helper_unverified", "发布清单没有包含自动热切换组件。")
            expected = _expected_digest(files[relative])
        if _hash(candidate) != expected:
            raise HotSwitchError("helper_unverified", "自动热切换组件校验失败，请重新取得完整程序包。")
        return candidate
    except HotSwitchError:
        raise
    except OSError as exc:
        raise HotSwitchError("helper_unverified", "无法读取并校验自动热切换组件。") from exc


def _ready_for_bridge(status):
    if status.get("recovery_pending") is True:
        if status.get("recovery_restart_required") is True:
            raise HotSwitchError(
                "restart_required",
                "Windows 仍标记手柄输入需要重启电脑。请保存工作并重启一次，重新连接手柄后再启动桥接。",
                details=status,
            )
        raise HotSwitchError(
            "recovery_pending",
            "热切换组件已运行，但上一次输入恢复尚未完成；请保持原手柄连接，等待自动恢复后再启动桥接。",
            details=status,
        )
    return status


def _install_locked(paths, client, notify, check_cancel, *, first_use):
    owner = current_user_sid()
    key = (str(paths.assets).casefold(), owner)
    if key in _attempted:
        raise HotSwitchError("setup_already_attempted", "本次安装未完成；重新打开程序后再试。")
    artifact = verify_helper_artifact(paths)
    check_cancel()
    notify("首次使用自动热切换，需要 Windows 确认安装受限组件；后续启动桥接无需再次确认。" if first_use else
           "需要 Windows 确认安装或更新热切换组件；安装程序会核验现有服务的归属和版本。")
    check_cancel()
    _attempted.add(key)
    try:
        code = run_installer(artifact, ["--install", "--owner-sid", owner])
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1223:
            raise HotSwitchError("setup_cancelled", "已取消安装自动热切换组件，桥接未启动。") from exc
        raise HotSwitchError("setup_failed", "自动热切换组件安装未完成，请重新打开程序后再试。") from exc
    check_cancel()
    if type(code) is not int or code != 0:
        raise HotSwitchError("setup_failed", "自动热切换组件安装未成功，桥接未启动。")
    if current_user_sid() != owner:
        raise HotSwitchError("owner_mismatch", "安装前后的 Windows 用户身份不一致，桥接未启动。")
    status = client.status(check_cancel)
    # Bind the final response to the pre-elevation account, and do not confuse
    # a running service with successful recovery of its previous transaction.
    return _ready_for_bridge(_status_reply(status, owner))


def ensure_hot_switch_service(paths, notify, check_cancel):
    """Return verified status; request one first-use UAC only if SCM is missing."""
    paths = as_paths(paths)
    check_cancel()
    client = HotSwitchClient(paths)
    try:
        return _ready_for_bridge(client.status(check_cancel))
    except HotSwitchError as exc:
        if exc.code != "service_missing":
            raise
    # A second startup cannot launch another installer while the first is open.
    while not _gate.acquire(timeout=0.1):
        check_cancel()
    try:
        check_cancel()
        try:
            return _ready_for_bridge(client.status(check_cancel))
        except HotSwitchError as exc:
            if exc.code != "service_missing":
                raise
        return _install_locked(paths, client, notify, check_cancel, first_use=True)
    finally:
        _gate.release()


def install_hot_switch_service(paths, notify, check_cancel):
    """Explicit installer command; may upgrade only native-validated predecessors.

    Do not call this as an automatic fallback for an arbitrary status failure.
    The native installer independently rejects another owner's/configuration's
    service even when an explicitly requested repair cannot query its old pipe.
    """
    paths = as_paths(paths)
    check_cancel()
    client = HotSwitchClient(paths)
    while not _gate.acquire(timeout=0.1):
        check_cancel()
    try:
        check_cancel()
        try:
            status = client.status(check_cancel)
        except HotSwitchError:
            return _install_locked(paths, client, notify, check_cancel, first_use=False)
        return _ready_for_bridge(status)
    finally:
        _gate.release()
