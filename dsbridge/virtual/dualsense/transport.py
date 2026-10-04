"""Local VIIPER V5 DualSense feeder, using only Python's standard library.

Protocol checked against hbashton/VIIPER commit
b78e31e93e84b4cb7c4b4c15b8aace10749106d2 (2026-10-03):
https://github.com/hbashton/VIIPER/blob/b78e31e93e84b4cb7c4b4c15b8aace10749106d2/docs/devices/dualsense.md
https://github.com/hbashton/VIIPER/blob/b78e31e93e84b4cb7c4b4c15b8aace10749106d2/device/dualsense/bthaptics.go

VIIPER and its signed usbip-win2 driver must already be installed and running.
This module neither installs a driver nor launches or configures VIIPER.
The game sees a virtual USB DualSense; physical XInput motors receive only an
RMS approximation of its native rear-channel waveform. Adaptive trigger
resistance, controller speaker and microphone are not forwarded.
"""

from __future__ import annotations

import ipaddress
import json
import math
import socket
import struct
import sys
import threading
import time
import uuid
import zlib
from collections.abc import Mapping


from dsbridge.virtual.dualsense.codec import (DEVICE_TYPE, FEEDBACK_SIZE, ATOMIC_SIZE,
    ViiperError, ProtocolError, FrameDecoder, encode_frame, encode_input, decode_haptic_pcm, decode_haptics)


class ViiperBackend:
    """Create/own one local virtual USB DualSense and translate its feedback.

    poll_feedback() returns (left large, right small), each in 0..1, when a
    change is available, otherwise None. Native waveform RMS and compatible
    rumble are composed using max, so a silent media lane cannot cancel rumble.
    Native haptics are an approximation on XInput motors, not lossless output.
    Connection/protocol failure raises ViiperError until stop()/start() is run.
    Explicit restart creates a fresh stream and resets all feedback generations.
    An optional persisted 32-hex controller_identity keeps Sony feature serial
    and MAC metadata stable between starts. None preserves per-start UUIDs.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 3242,
                 haptics_gain: float = 1.0, *, timeout: float = 5.0,
                 haptics_timeout: float = 0.05, require_attachment: bool = True,
                 device_type: str = DEVICE_TYPE,
                 controller_identity: str | None = None, feedback_sink=None):
        if host.lower() != "localhost":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise ValueError("VIIPER backend is restricted to localhost")
            except ValueError as exc:
                raise ValueError("VIIPER backend requires a loopback IP or localhost") from exc
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("VIIPER port must be in 1..65535")
        if not math.isfinite(haptics_gain) or not 0 <= haptics_gain <= 10:
            raise ValueError("haptics_gain must be finite and in 0..10")
        if timeout <= 0 or not 0.01 <= haptics_timeout <= 0.5:
            raise ValueError("Invalid VIIPER timeout")
        if device_type not in (DEVICE_TYPE, DEVICE_TYPE + "events"):
            raise ValueError("Only verified V5 DualSense HID+audio aliases are supported")
        if controller_identity is not None:
            if (not isinstance(controller_identity, str) or len(controller_identity) != 32
                    or any(character not in "0123456789abcdefABCDEF" for character in controller_identity)):
                raise ValueError("controller_identity must contain exactly 32 ASCII hexadecimal characters")
            controller_identity = controller_identity.upper()
        self.host, self.port = host, port
        self.haptics_gain = haptics_gain
        self.timeout, self.haptics_timeout = timeout, haptics_timeout
        self.require_attachment, self.device_type = require_attachment, device_type
        self.controller_identity = controller_identity
        # Optional receiver-only PCM/control consumer; never used by Bluetooth.
        self.feedback_sink = feedback_sink
        self.server_version = None
        self.bus_id = self.device_id = self.usbip_port = None
        self.on_attachment = self.before_remove = None
        self.last_cleanup_error = None
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._failure = None
        self._sequence = 0
        self._rumble = (0.0, 0.0)
        self._haptics = (0.0, 0.0)
        self._haptics_at = 0.0
        self._last_returned = None
        self._realtime_seen = False
        self._frames = self._native_frames = self._control_frames = 0
        self._native_nonzero = self._rumble_nonzero = 0
        self._native_peak = self._rumble_peak = (0.0, 0.0)

    def _management(self, command: str) -> dict:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
                conn.sendall(command.encode("utf-8") + b"\0")
                response = bytearray()
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > 65536:
                        raise ProtocolError("VIIPER management response exceeds 64 KiB")
            result = json.loads(response)
        except (OSError, ValueError) as exc:
            raise ViiperError(
                f"无法访问 VIIPER 本机 API {self.host}:{self.port}：{exc}。"
                "需要安装匹配的 hbashton VIIPER 与 usbip-win2 x64 驱动，"
                "重启后启动 VIIPER；本程序不会自动安装驱动。"
            ) from exc
        if not isinstance(result, dict):
            raise ProtocolError("VIIPER management response is not a JSON object")
        if result.get("status") or result.get("title"):
            raise ViiperError(f"VIIPER API {command.split(' ', 1)[0]}: "
                              f"{result.get('status', '')} {result.get('title', '')} "
                              f"{result.get('detail', '')}")
        return result

    def start(self) -> None:
        if self._socket is not None or self.bus_id is not None:
            raise ViiperError("VIIPER backend already started; call stop() before reconnecting")
        self._stop.clear()
        with self._lock:
            self._failure = None
            self._sequence = 0
            self._rumble = self._haptics = (0.0, 0.0)
            self._haptics_at = 0.0
            self._last_returned = None
            self._realtime_seen = False
            self._frames = self._native_frames = self._control_frames = 0
            self._native_nonzero = self._rumble_nonzero = 0
            self._native_peak = self._rumble_peak = (0.0, 0.0)
        try:
            ping = self._management("ping")
            if ping.get("server") != "VIIPER":
                raise ProtocolError("本机端口上的服务不是 VIIPER")
            self.server_version = ping.get("version", "unknown")
            created = self._management("bus/create")
            bus_id = created.get("busId")
            if not isinstance(bus_id, int) or not 1 <= bus_id <= 0xFFFFFFFF:
                raise ProtocolError("VIIPER returned an invalid bus ID")
            self.bus_id = bus_id
            identity = self.controller_identity or uuid.uuid4().hex.upper()
            options = {"type": self.device_type, "deviceSpecific": {
                "serial_number": "APEX01" + identity[:10],
                "mac_address": "02:" + ":".join(identity[i:i + 2] for i in range(0, 10, 2))}}
            device = self._management(f"bus/{bus_id}/add " + json.dumps(options, separators=(",", ":")))
            device_id = str(device.get("devId", ""))
            if (not device_id.isdecimal() or device.get("busId") != bus_id
                    or device.get("type") != self.device_type
                    or str(device.get("vid", "")).lower() != "0x054c"
                    or str(device.get("pid", "")).lower() != "0x0ce6"):
                raise ProtocolError("VIIPER returned an unexpected DualSense identity")
            self.device_id = device_id
            self.usbip_port = device.get("usbipPort")
            if self.on_attachment:
                self.on_attachment(bus_id, device_id, self.usbip_port)
            if self.require_attachment and sys.platform == "win32":
                if not isinstance(self.usbip_port, int) or self.usbip_port <= 0:
                    raise ViiperError("VIIPER 已创建设备，但没有确认 Windows USB/IP 挂载。"
                                      "请使用已安装驱动的 VIIPER 并启用本机 native auto-attach；"
                                      "只有 TCP 服务不足以让游戏识别 DualSense。")
            conn = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._socket = conn
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(0.2)
            conn.sendall(f"bus/{bus_id}/{device_id}\0".encode("ascii"))
            self._thread = threading.Thread(target=self._receive, name="VIIPER-feedback", daemon=True)
            self._thread.start()
            self.update({})
        except Exception:
            self.stop()
            raise

    def _fail(self, error: Exception) -> None:
        with self._lock:
            if not self._stop.is_set():
                self._failure = error if isinstance(error, ViiperError) else ViiperError(str(error))
            self._rumble = self._haptics = (0.0, 0.0)
            self._haptics_at = 0.0
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise ViiperError(f"VIIPER 反馈连接已停止：{failure}") from failure

    def update(self, state: Mapping) -> None:
        self._raise_if_failed()
        payload = encode_input(state)
        with self._send_lock:
            conn = self._socket
            if conn is None or self._stop.is_set():
                raise ViiperError("VIIPER backend is not running")
            packet = encode_frame(0x01, payload, self._sequence)
            try:
                conn.sendall(packet)
                self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            except OSError as exc:
                self._fail(exc)
                self._raise_if_failed()
                raise ViiperError("VIIPER input connection closed") from exc

    def _receive(self) -> None:
        decoder = FrameDecoder(events=self.device_type.endswith("events"))
        incomplete_since = None
        try:
            while not self._stop.is_set():
                try:
                    data = self._socket.recv(8192)
                except socket.timeout:
                    if incomplete_since is not None and time.monotonic() - incomplete_since > self.timeout:
                        raise ProtocolError("VIIPER 反馈帧接收超时，载荷不完整。已停止震动。")
                    continue
                if not data:
                    raise ViiperError("VIIPER stream disconnected; restart the bridge to reconnect")
                for kind, payload in decoder.feed(data):
                    self._apply_feedback(kind, payload)
                if decoder.buffer:
                    if incomplete_since is None:
                        incomplete_since = time.monotonic()
                else:
                    incomplete_since = None
        except Exception as exc:
            self._fail(exc)

    def _apply_feedback(self, kind: int, payload: bytes) -> None:
        if kind == 0x85:
            if len(payload) != 9 or payload[0] not in (0, 1):
                raise ProtocolError("Invalid VIIPER microphone lifecycle event")
            return  # This program does not synthesize microphone input.
        if kind == 0x83:
            if len(payload) != ATOMIC_SIZE or struct.unpack_from("<H", payload)[0] != FEEDBACK_SIZE:
                raise ProtocolError("Invalid VIIPER V5 atomic audio feedback")
            feedback = payload[2:2 + FEEDBACK_SIZE]
            # Front-channel speaker PCM is never interpreted as motor energy.
        elif kind in (0x81, 0x84) and len(payload) == FEEDBACK_SIZE:
            feedback = payload
        else:
            raise ProtocolError("Invalid VIIPER feedback")
        native = decode_haptics(feedback)
        if kind in (0x83, 0x84) and native is None:
            raise ProtocolError("VIIPER media feedback is missing its verified haptics carrier")
        now = time.monotonic()
        accepted_native = False
        with self._lock:
            self._frames += 1
            if kind == 0x81:
                raw = feedback[28:76]
                # Validity bits identify game-authored updates. LED/trigger-only
                # reports cannot erase rumble; media snapshots cannot replay it.
                if raw[0] == 0x02 and (raw[1] & 0x03 or raw[39] & 0x0C):
                    compatible = bool(raw[1] & 0x01 or raw[39] & 0x04)
                    self._rumble = (feedback[1] / 255, feedback[0] / 255) if compatible else (0.0, 0.0)
                    self._control_frames += 1
                    if any(self._rumble):
                        self._rumble_nonzero += 1
                    self._rumble_peak = tuple(max(self._rumble_peak[lane], self._rumble[lane]) for lane in (0, 1))
            if kind == 0x84:
                self._realtime_seen = True
            if native is not None and (kind == 0x84 or (kind == 0x83 and not self._realtime_seen)):
                accepted_native = True
                self._haptics = tuple(min(1.0, value * self.haptics_gain) for value in native)
                self._haptics_at = now
                self._native_frames += 1
                if any(native):
                    self._native_nonzero += 1
                self._native_peak = tuple(max(self._native_peak[lane], native[lane]) for lane in (0, 1))
        if self.feedback_sink is not None and (kind == 0x81 or accepted_native):
            # Enqueue only. HID/speaker snapshots cannot duplicate the PCM lane.
            self.feedback_sink(kind, feedback, now)

    def poll_feedback(self) -> tuple[float, float] | None:
        self._raise_if_failed()
        with self._lock:
            haptics = self._haptics if time.monotonic() - self._haptics_at <= self.haptics_timeout else (0.0, 0.0)
            feedback = tuple(max(self._rumble[lane], haptics[lane]) for lane in (0, 1))
            if feedback == self._last_returned:
                return None
            self._last_returned = feedback
            return feedback

    def status(self) -> dict:
        with self._lock:
            return {"server_version": self.server_version, "bus_id": self.bus_id,
                    "device_id": self.device_id, "usbip_port": self.usbip_port,
                    "device_type": self.device_type,
                    "running": self._socket is not None and self._failure is None and not self._stop.is_set(),
                    "feedback_frames": self._frames, "native_haptics_frames": self._native_frames,
                    "rumble_commands": self._control_frames,
                    "native_haptics_nonzero_frames": self._native_nonzero,
                    "native_haptics_peak_rms": self._native_peak,
                    "rumble_nonzero_commands": self._rumble_nonzero,
                    "rumble_peak": self._rumble_peak,
                    "native_haptics_translation": "rear-channel PCM RMS -> XInput motors (approximation)",
                    "error": str(self._failure) if self._failure else None}

    @property
    def error(self) -> Exception | None:
        """Nonblocking live error for the application's stop-vibration path."""
        with self._lock:
            return self._failure

    def stop(self) -> None:
        self._stop.set()
        with self._send_lock:
            conn, self._socket = self._socket, None
            if conn is not None:
                try:
                    conn.sendall(encode_frame(0x01, encode_input({}), self._sequence))
                except OSError:
                    pass
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        bus_id, self.bus_id = self.bus_id, None
        device_id, self.device_id = self.device_id, None
        usbip_port = self.usbip_port
        self.usbip_port = None
        # The dedicated bus is created by this instance; never touch other buses.
        if bus_id is not None:
            if self.before_remove:
                try:
                    if device_id is not None:
                        self.before_remove(bus_id, device_id, usbip_port)
                    else:
                        # A failed auto-attach can still leave a device/retry on
                        # our dedicated bus. Enumerate only that owned bus.
                        devices = self._management(f"bus/{bus_id}/list").get("devices", [])
                        for device in devices:
                            if device.get("busId") == bus_id and str(device.get("devId", "")).isdecimal():
                                self.before_remove(bus_id, str(device["devId"]), device.get("usbipPort"))
                except Exception as exc:
                    self.last_cleanup_error = str(exc)
            if device_id is not None:
                try:
                    self._management(f"bus/{bus_id}/remove {device_id}")
                except ViiperError:
                    pass
            try:
                self._management(f"bus/remove {bus_id}")
            except ViiperError:
                pass  # Server may already have removed the disconnected device.
        with self._lock:
            self._rumble = self._haptics = (0.0, 0.0)
            self._haptics_at = 0.0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()
