"""Windows audio defaults, without recording audio or disabling endpoints.

MMDevice methods follow the Microsoft Core Audio interfaces. The Windows
PolicyConfig ABI is also used by SoundSwitch:
https://github.com/Belphemur/SoundSwitch/blob/dev/SoundSwitch.Audio.Manager/Interop/Interface/Policy/IPolicyConfig.cs
Only SetDefaultEndpoint (slot 13) is used; no device formats are changed.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
from pathlib import Path
import sys
import threading
import time
import uuid

FLOWS = {"render": 0, "capture": 1}
ROLES = (0, 1, 2)  # Console, multimedia, communications.
HRESULT = C.c_int32
DWORD = C.c_uint32


class AudioDefaultError(RuntimeError):
    pass


class GUID(C.Structure):
    _fields_ = [("a", DWORD), ("b", C.c_uint16), ("c", C.c_uint16),
                ("d", C.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PropertyKey(C.Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


class _Array(C.Structure):
    _fields_ = [("count", DWORD), ("data", C.c_void_p)]


class _Value(C.Union):
    _fields_ = [("pointer", C.c_void_p), ("integer", C.c_int64), ("array", _Array)]


class PropVariant(C.Structure):
    _fields_ = [("vt", C.c_uint16), ("reserved", C.c_uint16 * 3), ("value", _Value)]


FRIENDLY_NAME = PropertyKey(GUID.parse("a45c254e-df1c-4efd-8020-67d146a850e0"), 14)
INSTANCE_ID = PropertyKey(GUID.parse("78c34fc8-104a-4aca-9ea4-524d52996e57"), 256)


def _check(hr, operation):
    if hr < 0:
        raise AudioDefaultError(f"{operation} 失败：0x{hr & 0xffffffff:08X}")


def is_sony_audio(endpoint):
    if not endpoint:
        return False
    return ("dualsense" in endpoint.get("name", "").casefold()
            or "vid_054c&pid_0ce6" in endpoint.get("instance_id", "").casefold())


class WindowsAudio:
    """One thread owns this COM apartment and all of its interface pointers."""
    def __init__(self):
        if sys.platform != "win32":
            raise AudioDefaultError("音频默认设备保护仅支持 Windows。")
        self.ole = C.WinDLL("ole32")
        self.ole.CoInitializeEx.argtypes = [C.c_void_p, DWORD]
        self.ole.CoInitializeEx.restype = HRESULT
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p, DWORD,
                                             C.POINTER(GUID), C.POINTER(C.c_void_p)]
        self.ole.CoCreateInstance.restype = HRESULT
        self.ole.CoTaskMemFree.argtypes = [C.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.ole.PropVariantClear.argtypes = [C.POINTER(PropVariant)]
        self.ole.PropVariantClear.restype = HRESULT
        hr = self.ole.CoInitializeEx(None, 0)
        self.owns_com = hr >= 0
        if not self.owns_com and hr & 0xffffffff != 0x80010106:
            _check(hr, "初始化音频 COM")
        self.enumerator = self.policy = None
        try:
            self.enumerator = self._create("bcde0395-e52f-467c-8e3d-c4579291692e",
                                           "a95664d2-9614-4f35-a746-de8db63617e6")
        except Exception:
            self.close()
            raise

    def _create(self, clsid, iid):
        result = C.c_void_p()
        _check(self.ole.CoCreateInstance(C.byref(GUID.parse(clsid)), None, 23,
                                         C.byref(GUID.parse(iid)), C.byref(result)), "创建音频接口")
        return result

    @staticmethod
    def _call(pointer, slot, arguments, *values, result_type=HRESULT):
        table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
        method = C.WINFUNCTYPE(result_type, C.c_void_p, *arguments)(table[slot])
        return method(pointer, *values)

    def _release(self, pointer):
        if pointer:
            self._call(pointer, 2, [], result_type=DWORD)

    def _id(self, device):
        value = C.c_void_p()
        _check(self._call(device, 5, [C.POINTER(C.c_void_p)], C.byref(value)), "读取音频 ID")
        try:
            return C.wstring_at(value)
        finally:
            self.ole.CoTaskMemFree(value)

    def _property(self, store, key):
        value = PropVariant()
        hr = self._call(store, 5, [C.POINTER(PropertyKey), C.POINTER(PropVariant)],
                        C.byref(key), C.byref(value))
        try:
            return C.wstring_at(value.value.pointer) if hr >= 0 and value.vt == 31 and value.value.pointer else ""
        finally:
            self.ole.PropVariantClear(C.byref(value))

    def _info(self, device, flow):
        store = C.c_void_p()
        _check(self._call(device, 4, [DWORD, C.POINTER(C.c_void_p)], 0, C.byref(store)), "读取音频属性")
        try:
            return {"id": self._id(device), "name": self._property(store, FRIENDLY_NAME),
                    "instance_id": self._property(store, INSTANCE_ID), "flow": flow}
        finally:
            self._release(store)

    def endpoints(self):
        result = []
        for flow, number in FLOWS.items():
            collection = C.c_void_p()
            _check(self._call(self.enumerator, 3, [C.c_int, DWORD, C.POINTER(C.c_void_p)],
                             number, 1, C.byref(collection)), "枚举音频设备")
            try:
                count = DWORD()
                _check(self._call(collection, 3, [C.POINTER(DWORD)], C.byref(count)), "读取音频设备数量")
                for index in range(count.value):
                    device = C.c_void_p()
                    _check(self._call(collection, 4, [DWORD, C.POINTER(C.c_void_p)],
                                     index, C.byref(device)), "读取音频设备")
                    try:
                        result.append(self._info(device, flow))
                    finally:
                        self._release(device)
            finally:
                self._release(collection)
        return result

    def default_id(self, flow, role):
        device = C.c_void_p()
        hr = self._call(self.enumerator, 4, [C.c_int, C.c_int, C.POINTER(C.c_void_p)],
                        FLOWS[flow], role, C.byref(device))
        if hr & 0xffffffff == 0x80070490:
            return None
        _check(hr, "读取默认音频设备")
        try:
            return self._id(device)
        finally:
            self._release(device)

    def snapshot(self):
        endpoints = self.endpoints()
        by_id = {item["id"]: item for item in endpoints}
        defaults = {f"{flow}:{role}": by_id.get(self.default_id(flow, role))
                    for flow in FLOWS for role in ROLES}
        return {"endpoints": endpoints, "defaults": defaults}

    def set_default(self, endpoint_id, flow, role):
        endpoint = next((item for item in self.endpoints() if item["id"] == endpoint_id), None)
        if not endpoint or endpoint["flow"] != flow or is_sony_audio(endpoint):
            raise AudioDefaultError("恢复目标不是当前可用的电脑音频设备。")
        if self.policy is None:
            self.policy = self._create("870af99c-171d-4f9e-af0d-e63df40c2bc9",
                                       "f8679f50-850a-41cf-9c72-430f290290c8")
        _check(self._call(self.policy, 13, [C.c_wchar_p, C.c_int], endpoint_id, role), "恢复默认音频设备")
        if self.default_id(flow, role) != endpoint_id:
            raise AudioDefaultError("Windows 未确认默认音频设备已恢复。")

    def close(self):
        self._release(self.policy)
        self._release(self.enumerator)
        self.policy = self.enumerator = None
        if self.owns_com:
            self.ole.CoUninitialize()
            self.owns_com = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def choose_fallback(endpoints, flow):
    candidates = [item for item in endpoints if item["flow"] == flow and not is_sony_audio(item)
                  and not any(word in item["name"].casefold() for word in
                              ("todesk", "virtual", "立体声混音", "stereo mix"))]
    preferred = [item for item in candidates if "realtek" in item["name"].casefold()]
    candidates = preferred or candidates
    if len(candidates) == 1:
        return candidates[0]
    kind = "扬声器" if flow == "render" else "麦克风"
    raise AudioDefaultError(f"无法唯一确定原来的{kind}，请先在 Windows 声音设置中选择："
                            + "、".join(item["name"] for item in candidates))


def repair_defaults(audio, remembered=None):
    before = audio.snapshot()
    remembered = remembered or {}
    active = {item["id"]: item for item in before["endpoints"]}
    changes = []
    for key, current in before["defaults"].items():
        if not is_sony_audio(current):
            continue
        flow, role = key.split(":")
        saved = remembered.get(key)
        target = active.get(saved["id"]) if saved else None
        if not target or target["flow"] != flow or is_sony_audio(target):
            target = choose_fallback(before["endpoints"], flow)
        audio.set_default(target["id"], flow, int(role))
        changes.append({"role": key, "from": current["name"], "to": target["name"]})
    return {"before": before, "after": audio.snapshot(), "changes": changes}


def _load_defaults(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("defaults"), dict):
            raise ValueError("unknown saved audio format")
        return {key: item for key, item in value["defaults"].items()
                if key in {f"{flow}:{role}" for flow in FLOWS for role in ROLES}
                and isinstance(item, dict) and isinstance(item.get("id"), str)
                and not is_sony_audio(item)}
    except (OSError, ValueError, AttributeError) as exc:
        raise AudioDefaultError("保存的音频设备记录无法读取，请先用 Windows 声音设置恢复设备。") from exc


def _save_defaults(path, defaults):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "defaults": defaults},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _remember(snapshot, remembered):
    result = dict(remembered)
    for key, endpoint in snapshot["defaults"].items():
        if endpoint and not is_sony_audio(endpoint):
            result[key] = dict(endpoint)
    return result


def repair_audio_defaults(base, audio_factory=WindowsAudio):
    """Explicit repair, also used on GUI startup before creating virtual USB."""
    path = Path(base) / "audio-defaults.json"
    remembered = _load_defaults(path)
    with audio_factory() as audio:
        report = repair_defaults(audio, remembered)
    remembered = _remember(report["after"], remembered)
    _save_defaults(path, remembered)
    (Path(base) / "audio-repair.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


class DefaultAudioGuard:
    """Protect all six audio defaults for the virtual-device lifetime.

    Defaults on real endpoints are accepted and remembered, including the
    user's manual changes while playing. Only a switch to DualSense triggers
    restoration. A unplugged saved device is never set as the default.
    """
    def __init__(self, base, *, audio_factory=WindowsAudio, interval=0.2):
        self.path = Path(base) / "audio-defaults.json"
        self.audio_factory = audio_factory
        self.interval = interval
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._failure = None
        self._defaults = {}
        self._restored = 0

    @property
    def error(self):
        with self._lock:
            return self._failure

    def status(self):
        with self._lock:
            return {"active": bool(self._thread and self._thread.is_alive()) and self._failure is None,
                    "restored_roles": self._restored,
                    "defaults": {key: endpoint["name"] for key, endpoint in self._defaults.items()},
                    "error": str(self._failure) if self._failure else None}

    def start(self):
        if self._thread and self._thread.is_alive():
            raise AudioDefaultError("默认音频保护已经在运行。")
        self._stop.clear()
        self._ready.clear()
        with self._lock:
            self._failure = None
            self._restored = 0
        self._thread = threading.Thread(target=self._run, name="Default audio protection", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise AudioDefaultError("默认音频设备检查超时，已取消虚拟手柄启动。")
        if self.error:
            raise self.error

    def _run(self):
        try:
            remembered = _load_defaults(self.path)
            with self.audio_factory() as audio:
                while True:
                    report = repair_defaults(audio, remembered)
                    updated = _remember(report["after"], remembered)
                    if updated != remembered:
                        _save_defaults(self.path, updated)
                        remembered = updated
                    with self._lock:
                        self._defaults = dict(remembered)
                        self._restored += len(report["changes"])
                    self._ready.set()
                    if self._stop.wait(self.interval):
                        # Device removal can itself trigger a default change.
                        final = repair_defaults(audio, remembered)
                        updated = _remember(final["after"], remembered)
                        if updated != remembered:
                            _save_defaults(self.path, updated)
                        with self._lock:
                            self._defaults = updated
                            self._restored += len(final["changes"])
                        break
        except Exception as exc:
            with self._lock:
                self._failure = exc
        finally:
            self._ready.set()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
            if self._thread.is_alive():
                raise AudioDefaultError("默认音频保护线程尚未退出。")


def main():
    parser = argparse.ArgumentParser(description="电脑默认扬声器和麦克风恢复工具")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.diagnose and not args.repair:
        parser.error("请选择 --diagnose（只读）或 --repair（恢复被DS抢占的默认音频）")
    if args.repair:
        from dsbridge.runtime.paths import current_paths
        report = repair_audio_defaults(current_paths().prepare().state)
    else:
        with WindowsAudio() as audio:
            report = audio.snapshot()
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
