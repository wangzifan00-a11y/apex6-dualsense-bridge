"""Explicit Unicode Windows elevation and installer process lifetime."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import subprocess

def run_installer(path: Path, arguments, *, verb="runas"):
    """Use the Unicode shell API, wait for the actual process, always close its handle.

    'open' is only used by the native process-handle test with a harmless child.
    Production always uses 'runas'. No shell command interpolation is involved.
    """
    class ExecuteInfo(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("fMask", W.ULONG), ("hwnd", W.HWND),
                    ("lpVerb", W.LPCWSTR), ("lpFile", W.LPCWSTR), ("lpParameters", W.LPCWSTR),
                    ("lpDirectory", W.LPCWSTR), ("nShow", C.c_int), ("hInstApp", W.HINSTANCE),
                    ("lpIDList", C.c_void_p), ("lpClass", W.LPCWSTR), ("hkeyClass", W.HKEY),
                    ("dwHotKey", W.DWORD), ("hIcon", W.HANDLE), ("hProcess", W.HANDLE)]
    shell = C.WinDLL("shell32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    ole = C.OleDLL("ole32")
    shell.ShellExecuteExW.argtypes = [C.POINTER(ExecuteInfo)]
    shell.ShellExecuteExW.restype = W.BOOL
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.WaitForSingleObject.restype = W.DWORD
    kernel.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    kernel.GetExitCodeProcess.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    ole.CoInitializeEx(None, 6)  # STA, disable OLE1 DDE. Installer runs in a worker.
    info = ExecuteInfo()
    info.cbSize = C.sizeof(info)
    info.fMask = 0x40 | 0x100 | 0x400  # process handle, synchronous launch, no error dialogs
    info.lpVerb, info.lpFile = verb, str(path)
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = str(path.parent)
    info.nShow = 0
    try:
        if not shell.ShellExecuteExW(C.byref(info)):
            raise C.WinError(C.get_last_error())
        if not info.hProcess:
            raise RuntimeError("安装程序未返回进程句柄，无法确认安装结果。")
        # No timeout/relaunch: an installer may wait for UAC or another MSI.
        if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) != 0:
            raise C.WinError(C.get_last_error())
        code = W.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, C.byref(code)):
            raise C.WinError(C.get_last_error())
        return code.value
    finally:
        if info.hProcess:
            kernel.CloseHandle(info.hProcess)
        ole.CoUninitialize()


