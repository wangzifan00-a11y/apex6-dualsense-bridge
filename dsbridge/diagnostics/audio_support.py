"""WASAPI helpers for explicit synthetic DualSense feedback tests only.

These helpers do not capture system audio. Normal bridge modes receive their
haptic stream through VIIPER; the test emitters use SoundCard to play known
four-channel signals into the newly created virtual Sony endpoint.
"""
from contextlib import contextmanager
import ctypes
import importlib
import sys
import threading

_soundcard_import_lock = threading.Lock()
_soundcard_module = None


@contextmanager
def _windows_com(coinit=0):
    if sys.platform != "win32":
        raise RuntimeError("DS 音轨自检仅支持 Windows。")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    result = ole32.CoInitializeEx(None, coinit)
    unsigned = result & 0xFFFFFFFF
    owns = result >= 0
    if not owns and unsigned != 0x80010106:
        raise OSError(f"无法初始化 DS 自检音频线程 COM：0x{unsigned:08X}")
    try:
        yield
    finally:
        if owns:
            ole32.CoUninitialize()


def _soundcard():
    global _soundcard_module
    if sys.platform != "win32":
        raise RuntimeError("DS 音轨自检仅支持 Windows。")
    with _soundcard_import_lock:
        if _soundcard_module is not None:
            return _soundcard_module
        result = {}
        def load():
            try:
                # SoundCard initializes its own COM apartment on import; use a
                # fresh STA to avoid its incorrect handling of MTA S_FALSE.
                with _windows_com(coinit=2):
                    result["module"] = importlib.import_module("soundcard")
            except Exception as exc:
                result["error"] = exc
        loader = threading.Thread(target=load, name="DS test audio import", daemon=True)
        loader.start()
        loader.join(timeout=5)
        if loader.is_alive():
            raise TimeoutError("DS 音轨自检组件加载超过 5 秒。")
        if "error" in result:
            raise result["error"]
        _soundcard_module = result["module"]
        return _soundcard_module
