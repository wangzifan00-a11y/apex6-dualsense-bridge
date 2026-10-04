"""Make only a server spawned by this bridge die with its owning process."""
import ctypes as C
from ctypes import wintypes as W


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                ("minimum_working_set", C.c_size_t), ("maximum_working_set", C.c_size_t),
                ("active_processes", W.DWORD), ("affinity", C.c_size_t),
                ("priority", W.DWORD), ("scheduling", W.DWORD)]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io_counters", C.c_uint64 * 6),
                ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class OwnedServerJob:
    def __init__(self):
        self.native = C.WinDLL("kernel32.dll", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [C.c_void_p, W.LPCWSTR], W.HANDLE),
            ("SetInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            ("AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
            ("CloseHandle", [W.HANDLE], W.BOOL),
        ):
            method = getattr(self.native, name)
            method.argtypes, method.restype = args, result
        self.handle = self.native.CreateJobObjectW(None, None)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.native.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
            error = C.get_last_error()
            self.close()
            raise C.WinError(error)

    def assign(self, process):
        if not self.native.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise C.WinError(C.get_last_error())

    def close(self):
        if self.handle:
            self.native.CloseHandle(self.handle)
            self.handle = None
