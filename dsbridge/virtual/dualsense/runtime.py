"""Run the pinned portable local server only for this bridge session."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
from dsbridge.runtime.paths import as_paths


class LocalViiper:
    def __init__(self, base: Path, config: dict, *, feedback_sink=None):
        from dsbridge.virtual.dualsense.transport import ViiperBackend
        host = config.get("host", "127.0.0.1")
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("VIIPER 只允许本机回环地址")
        self.host, self.port = host, int(config.get("port", 3242))
        if not 1 <= self.port <= 65535:
            raise ValueError("VIIPER 端口无效")
        self.paths = as_paths(base)
        self.base, self.config = self.paths.state, dict(config)
        # Keep Sony feature identity stable across sessions for game caches.
        identity_file = self.base / "controller-identity.json"
        if "controller_identity" not in self.config:
            if identity_file.exists():
                identity = json.loads(identity_file.read_text(encoding="utf-8"))["identity"]
            else:
                identity = uuid.uuid4().hex
                identity_file.write_text(json.dumps({"identity": identity}, indent=2), encoding="utf-8")
            self.config["controller_identity"] = identity
        self.backend = ViiperBackend(**self.config, feedback_sink=feedback_sink)
        self.backend.on_attachment = self._record_usb
        self.backend.before_remove = self._cleanup_usb
        self.process = None
        self.log_file = None
        self.audio_guard = None
        self.child_job = None
        self.last_cleanup_error = None

    def _record_usb(self, bus, device, port):
        from dsbridge.platform.windows.usbip import record_attachment
        record_attachment(self.base, bus, device, port)

    def _cleanup_usb(self, bus, device, port):
        from dsbridge.platform.windows.usbip import record_attachment, recover_export
        record_attachment(self.base, bus, device, port)
        recover_export(self.base)

    @property
    def error(self):
        if self.audio_guard and self.audio_guard.error:
            return RuntimeError("默认音频保护失败：" + str(self.audio_guard.error))
        if self.process is not None and self.process.poll() is not None:
            return RuntimeError("VIIPER 服务器已退出；请查看 logs/viiper.log")
        return self.backend.error

    def _listening(self):
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return True
        except OSError:
            return False

    def start(self):
        try:
            from dsbridge.platform.windows.audio import DefaultAudioGuard
            self.audio_guard = DefaultAudioGuard(self.base)
            # Snapshot/repair BEFORE Windows sees the new audio endpoints.
            self.audio_guard.start()
            if not self._listening():
                # The exact upstream release rejects other USB/IP ABIs.
                program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")
                driver_cli = Path(program_files) / "USBip/usbip.exe"
                if not driver_cli.is_file():
                    raise RuntimeError("PS5 模式尚缺 usbip-win2 0.9.7.7 驱动。请先运行准备好的驱动安装入口。")
                check = subprocess.run([str(driver_cli), "--version"], capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                if check.returncode or check.stdout.decode("utf-8", "replace").strip() != "0.9.7.7":
                    raise RuntimeError("USB/IP 驱动版本不匹配；此程序要求 0.9.7.7，不自动替换现有驱动。")
                executable = self.paths.assets / "vendor/viiper/viiper.exe"
                expected_file = executable.with_suffix(".sha256")
                if not executable.is_file() or not expected_file.is_file():
                    raise RuntimeError("缺少完整的 VIIPER 程序文件，请保留整个发布目录。")
                if hashlib.sha256(executable.read_bytes()).hexdigest() != expected_file.read_text().strip():
                    raise RuntimeError("VIIPER 文件哈希校验失败，请重新取得完整发布目录。")
                log_dir = self.base / "logs"
                log_dir.mkdir(exist_ok=True)
                self.log_file = (log_dir / "viiper.log").open("ab")
                args = [str(executable), "server", "--api.addr=" + self.host + ":" + str(self.port),
                        "--usb.addr=127.0.0.1:3241", "--api.auto-attach-local-client=true",
                        "--api.auto-attach-windows-native=true", "--api.require-local-host-auth=false"]
                from dsbridge.platform.windows.process import OwnedServerJob
                self.child_job = OwnedServerJob()
                self.process = subprocess.Popen(args, stdout=self.log_file, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                self.child_job.assign(self.process)
                deadline = time.monotonic() + 12
                while not self._listening():
                    if self.process.poll() is not None:
                        raise RuntimeError("VIIPER 启动失败，驱动可能需重启后生效。详见 logs/viiper.log。")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("VIIPER 启动超时，详见 logs/viiper.log。")
                    time.sleep(0.1)
            self.backend.start()
            if self.audio_guard.error:
                raise self.audio_guard.error
        except Exception:
            self.stop()
            raise

    def update(self, state):
        self.backend.update(state)

    def poll_feedback(self):
        return self.backend.poll_feedback()

    def status(self):
        status = self.backend.status()
        status["audio_protection"] = self.audio_guard.status() if self.audio_guard else None
        return status

    def stop(self):
        try:
            self.backend.stop()
            self.last_cleanup_error = getattr(self.backend, "last_cleanup_error", None)
        finally:
            try:
                if self.process is not None:
                    # Never inspect or terminate another application's server.
                    if self.process.poll() is None:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=3)
                    self.process = None
                if self.log_file:
                    self.log_file.close()
                    self.log_file = None
            finally:
                if self.child_job:
                    self.child_job.close()
                    self.child_job = None
                # Keep protection through device removal, then final readback.
                if self.audio_guard:
                    self.audio_guard.stop()
                    self.audio_guard = None
