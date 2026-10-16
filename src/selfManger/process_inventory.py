"""Read process names without fetching every process's path or command line."""
from __future__ import annotations

import sys


def process_names():
    if sys.platform != "win32":
        import psutil
        for process in psutil.process_iter(["pid", "name"]):
            yield process.info["pid"], process.info.get("name") or ""
        return

    # psutil's Windows name() resolves the executable of every process, holding
    # the GIL during that native lookup. Toolhelp returns all names in one snapshot.
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for method in (kernel.Process32FirstW, kernel.Process32NextW):
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        method.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            yield entry.th32ProcessID, entry.szExeFile
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error not in (0, 18):  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(snapshot)
