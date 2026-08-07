from __future__ import annotations

import ctypes
import stat
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from hermes_connector.domain.local_gateway import ProcessIdentityEvidence

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH_CHARS = 32768
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class ProcessIdentityProvider(Protocol):
    def __call__(self, pid: int) -> ProcessIdentityEvidence | None: ...


class _FileTime(ctypes.Structure):
    _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


def _kernel32() -> object:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return kernel32


def _filetime_value(value: _FileTime) -> int:
    return (int(value.high) << 32) | int(value.low)


def _snapshot_process(kernel32: object, pid: int) -> tuple[int, Path] | None:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        return None
    try:
        created = _FileTime()
        exited = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        creation_ticks = _filetime_value(created)
        if creation_ticks <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(length),
        ):
            return None
        if not 1 <= length.value < len(buffer):
            return None
        executable = Path(buffer.value)
        if (
            not executable.is_absolute()
            or ".." in executable.parts
            or "\x00" in str(executable)
        ):
            return None
        return creation_ticks * 100, executable
    finally:
        kernel32.CloseHandle(handle)


def current_process_identity(pid: int) -> ProcessIdentityEvidence | None:
    """Return immutable Windows process evidence or fail closed."""

    if type(pid) is not int or pid <= 0:
        return None
    try:
        kernel32 = _kernel32()
        first = _snapshot_process(kernel32, pid)
        if first is None:
            return None
        start_time_ns, executable = first
        metadata = executable.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_ino <= 0:
            return None
        second = _snapshot_process(kernel32, pid)
        if second != first:
            return None
        return ProcessIdentityEvidence(
            start_time_ns=start_time_ns,
            executable_path=executable,
            executable_device=int(metadata.st_dev),
            executable_inode=int(metadata.st_ino),
        )
    except (OSError, UnicodeError, ValueError):
        return None


def normalize_process_identity(value: object) -> ProcessIdentityEvidence | None:
    try:
        start_time_ns = value.start_time_ns
        executable_path = Path(value.executable_path)
        executable_device = value.executable_device
        executable_inode = value.executable_inode
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        type(start_time_ns) is not int
        or start_time_ns <= 0
        or type(executable_device) is not int
        or executable_device < 0
        or type(executable_inode) is not int
        or executable_inode <= 0
        or not executable_path.is_absolute()
        or ".." in executable_path.parts
        or "\x00" in str(executable_path)
    ):
        return None
    return ProcessIdentityEvidence(
        start_time_ns=start_time_ns,
        executable_path=executable_path,
        executable_device=executable_device,
        executable_inode=executable_inode,
    )


__all__ = [
    "ProcessIdentityProvider",
    "current_process_identity",
    "normalize_process_identity",
]
