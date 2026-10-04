"""Exclusive, reversible APEX 6 vendor-waveform session.

The persistent RAM block is read only. Commands 53 change volatile routing;
command A4 / profile saving is deliberately absent.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time

from dsbridge.controllers.flydigi.apex6.hid import ReceiverError, ReceiverHID, USAGE_PAGE, receiver_interfaces
from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol, waveform_report

ZERO = (0,) * 8


def saved_motor_modes(mapping):
    """RAM 6: persistent area 0, then volatile area 1 at offset 32.

    Hardware readbacks establish that area-1 mode 0 releases its override; the
    pad resumes the untouched persistent preset. Area-0 mode numbers must not
    be replayed into area 1, where they have different meanings.
    """
    if len(mapping) != 64 or mapping[0] != 2 or mapping[32] != 2:
        raise ReceiverError("无法恢复此版本的马达配置，未启用波形模式")
    result = []
    for target, offset in ((0, 33), (1, 44)):
        entry = mapping[offset:offset + 11]
        if entry[0] == 0 and not any(entry[1:]):
            result.append((target, 0, b""))
        else:
            raise ReceiverError("另一个程序正在使用扳机的临时输出模式，请先停止它")
    for target, offset in ((0x10, 55), (0x11, 57)):
        mode, gain = mapping[offset:offset + 2]
        if mode or gain:
            raise ReceiverError("另一个程序正在使用握把的临时输出模式，请先停止它")
        result.append((target, 0, b""))
    return result


class ReceiverClaim:
    """Named mutex shared by all builds and standalone receiver tests."""
    def __init__(self):
        self.native = C.WinDLL("kernel32.dll", use_last_error=True)
        self.native.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
        self.native.CreateMutexW.restype = W.HANDLE
        self.native.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self.native.WaitForSingleObject.restype = W.DWORD
        self.native.ReleaseMutex.argtypes = [W.HANDLE]
        self.native.CloseHandle.argtypes = [W.HANDLE]
        self.handle = self.native.CreateMutexW(None, False, "Local\\OctopusBridge.Apex6Waveform")
        if not self.handle:
            raise ReceiverError("无法建立接收器输出锁")
        if self.native.WaitForSingleObject(self.handle, 0) not in (0, 0x80):
            self.native.CloseHandle(self.handle)
            self.handle = None
            raise ReceiverError("接收器正在被另一份震动桥或测试使用，请先停止该转换")

    def close(self):
        if self.handle:
            self.native.ReleaseMutex(self.handle)
            self.native.CloseHandle(self.handle)
            self.handle = None


class Apex6Session:
    def __init__(self, base, *, transport_factory=ReceiverHID, claim_factory=ReceiverClaim):
        self.base = Path(base)
        self.journal_path = self.base / "receiver-session.json"
        self.transport_factory, self.claim_factory = transport_factory, claim_factory
        self.claim = self.transport = self.protocol = None
        self.identity = self.slot = self.mapping = None
        self.modes = None
        self.touched = False
        self.journal = None
        self.packet_count = 0
        self.cleanup_errors = []
        self.restored = False

    def _save(self):
        self.journal["updated_at"] = datetime.now(timezone.utc).isoformat()
        temp = self.journal_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.journal, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.journal_path)

    def start(self, path=None):
        self.claim = self.claim_factory()
        try:
            if path is None:
                candidates = [row for row in receiver_interfaces()
                              if row.get("usage_page") == USAGE_PAGE
                              and row.get("output_length") == row.get("input_length") == 33]
                if len(candidates) != 1:
                    raise ReceiverError("请连接唯一的 APEX 6 接收器，并保持手柄已唤醒")
                path = candidates[0]["path"]
            self.transport = self.transport_factory(path)
            self.protocol = ReceiverProtocol(self.transport)
            self.identity = self.protocol.identity()
            self.slot, self.mapping = self.protocol.motor_mapping()
            if self.journal_path.exists():
                old = json.loads(self.journal_path.read_text(encoding="utf-8"))
                if old.get("restore_pending"):
                    if old.get("format") != 1 or old.get("uid") != self.identity["uid"]:
                        raise ReceiverError("另一个手柄的恢复记录尚未处理；不会向当前手柄重放旧设置")
                    saved = bytes.fromhex(old["motor_mapping_hex"])
                    if old.get("slot") != self.slot or saved[:32] != self.mapping[:32]:
                        raise ReceiverError("上次异常退出后手柄预设已改变，需先恢复后再启动")
                    self._restore_modes(saved_motor_modes(saved))
                    self.slot, self.mapping = self.protocol.motor_mapping()
                    if self.mapping != saved:
                        raise ReceiverError("上次临时马达配置尚未恢复，暂不启动新转换")
                    old["restore_pending"] = False
                    self.journal = old
                    self._save()
            self.modes = saved_motor_modes(self.mapping)
            self.journal = {"format": 1, "restore_pending": True,
                            "uid": self.identity["uid"], "slot": self.slot,
                            "motor_mapping_hex": self.mapping.hex(),
                            "started_at": datetime.now(timezone.utc).isoformat()}
            self._save()
            # Mark before I/O: a lost ACK may follow a successful firmware write.
            self.touched = True
            # This receiver ACKs individual targets, not group commands.
            for target in (0, 1):
                self.protocol.mode(target, 3, b"\0\x40")
            for target in (0x10, 0x11):
                self.protocol.mode(target, 2, b"\x40\0")
            time.sleep(0.1)
            self.neutral()
        except Exception:
            self.stop()
            raise

    def send(self, left=ZERO, right=ZERO, trigger=ZERO, *, selector=3, trigger_enabled=False):
        self.transport.write(waveform_report(left, right, trigger, selector=selector,
                                             trigger_enabled=trigger_enabled))
        self.packet_count += 1

    def neutral(self):
        self.send(selector=2, trigger_enabled=True)

    def check_identity(self):
        if self.protocol.identity()["uid"] != self.identity["uid"]:
            raise ReceiverError("接收器连接的手柄已更换，转换已停止")
        if self.protocol.slot() != self.slot:
            raise ReceiverError("手柄配置槽已更换，请重新启动转换")

    def _restore_modes(self, modes):
        errors = []
        for target, mode, parameters in modes:
            try:
                self.protocol.mode(target, mode, parameters)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise ReceiverError("；".join(errors))

    def stop(self):
        try:
            if self.transport and self.touched:
                try:
                    self.neutral()
                except Exception as exc:
                    self.cleanup_errors.append(str(exc))
                # A replacement pad must never receive the previous pad's settings.
                try:
                    flush = getattr(self.transport, "flush_queue", None)
                    if flush:
                        flush()  # Monitor has stopped; require fresh identity/RAM.
                    if self.protocol.identity()["uid"] != self.identity["uid"]:
                        raise ReceiverError("手柄身份改变，保留恢复记录，未写入旧设置")
                    slot, mapping = self.protocol.motor_mapping()
                    if slot != self.slot or mapping[:32] != self.mapping[:32]:
                        raise ReceiverError("转换期间手柄预设已更改，未重放旧配置")
                except Exception as exc:
                    self.cleanup_errors.append(str(exc))
                    return
                for action in (lambda: self._restore_modes(self.modes),):
                    try:
                        action()
                    except Exception as exc:
                        self.cleanup_errors.append(str(exc))
                after_slot, after_mapping = None, None
                try:
                    after_slot, after_mapping = self.protocol.motor_mapping()
                    if after_slot != self.slot or after_mapping != self.mapping:
                        raise ReceiverError("马达配置恢复后的读取结果与启动前不一致")
                except Exception as exc:
                    self.cleanup_errors.append(str(exc))
                self.touched = False
                self.restored = not self.cleanup_errors
                self.journal["restore_pending"] = not self.restored
                self.journal["restored_slot"] = after_slot
                self.journal["restored_mapping_hex"] = after_mapping.hex() if after_mapping is not None else None
                self.journal["cleanup_errors"] = self.cleanup_errors
                self._save()
        finally:
            if self.transport:
                self.transport.close()
                self.transport = None
            if self.claim:
                self.claim.close()
                self.claim = None


def recover_pending_motors(base):
    """Recover a dead owner's volatile routing without starting new waveforms."""
    path = Path(base) / "receiver-session.json"
    if not path.exists():
        return False
    old = json.loads(path.read_text(encoding="utf-8"))
    if not old.get("restore_pending"):
        return False
    if old.get("format") != 1:
        raise ReceiverError("马达恢复记录格式异常")
    saved = bytes.fromhex(old["motor_mapping_hex"])
    modes = saved_motor_modes(saved)
    session = Apex6Session(base)
    try:
        session.claim = ReceiverClaim()
        candidates = [row for row in receiver_interfaces()
                      if row.get("usage_page") == USAGE_PAGE
                      and row.get("output_length") == row.get("input_length") == 33]
        if len(candidates) != 1:
            raise ReceiverError("接收器未连接；普通输入仍会自动恢复")
        session.transport = ReceiverHID(candidates[0]["path"])
        session.protocol = ReceiverProtocol(session.transport)
        identity = session.protocol.identity()
        slot, mapping = session.protocol.motor_mapping()
        if identity["uid"] != old["uid"] or slot != old["slot"] or mapping[:32] != saved[:32]:
            raise ReceiverError("手柄或预设已改变，未向其写入旧的马达设置")
        session.identity, session.slot, session.mapping = identity, slot, saved
        session.modes, session.journal, session.touched = modes, old, True
    finally:
        session.stop()
    if not session.restored:
        raise ReceiverError("；".join(session.cleanup_errors) or "马达恢复待重试")
    return True


def motor_test(base, *, notify=lambda message: None, cancel=None):
    """Four short, low-level tones; always stop and restore on the owning thread."""
    cancel = cancel or threading.Event()
    session = Apex6Session(base)
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "positions": []}
    try:
        session.start()
        result["identity"] = session.identity
        for motor, name in enumerate(("左握把", "右握把", "左扳机", "右扳机")):
            if cancel.is_set():
                break
            notify("轻震测试：" + name)
            start = deadline = time.perf_counter()
            frame = 0
            while time.perf_counter() - start < 0.48 and not cancel.is_set():
                tone = tuple(round(25 * math.sin(2 * math.pi * 90 * (frame * 8 + i) / 1000)) for i in range(8))
                session.send(tone if motor == 0 else ZERO, tone if motor == 1 else ZERO,
                             tone if motor >= 2 else ZERO,
                             selector=motor - 2 if motor >= 2 else 3,
                             trigger_enabled=motor >= 2)
                frame += 1
                deadline += 0.008
                cancel.wait(max(0, deadline - time.perf_counter()))
            session.neutral()
            result["positions"].append({"name": name, "waveform_packets": frame})
            cancel.wait(0.85)
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        session.stop()
        result.update(motor_packets=session.packet_count, restored=session.restored,
                      cleanup_errors=session.cleanup_errors)
        (Path(base) / "receiver-motor-test.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("error") or result["cleanup_errors"]:
        raise ReceiverError(result.get("error") or "；".join(result["cleanup_errors"]))
    return result
