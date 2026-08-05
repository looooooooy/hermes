from __future__ import annotations

import ctypes
import ctypes.util
import os
import stat
from pathlib import Path
from typing import Protocol

from hermes_connector.domain.local_gateway import ProcessIdentityEvidence

_PROC_PIDTBSDINFO = 3
_PROC_PIDPATHINFO_MAXSIZE = 4096


class ProcessIdentityProvider(Protocol):
    def __call__(self, pid: int) -> ProcessIdentityEvidence | None: ...


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = (
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    )


def _proc_bsd_snapshot(library: object, pid: int) -> tuple[int, int] | None:
    bsd = _ProcBSDInfo()
    bsd_size = ctypes.sizeof(bsd)
    if (
        library.proc_pidinfo(
            pid,
            _PROC_PIDTBSDINFO,
            0,
            ctypes.byref(bsd),
            bsd_size,
        )
        != bsd_size
        or bsd.pbi_pid != pid
        or bsd.pbi_start_tvsec <= 0
        or not 0 <= bsd.pbi_start_tvusec < 1_000_000
    ):
        return None
    return bsd.pbi_start_tvsec, bsd.pbi_start_tvusec


def _proc_executable_path(library: object, pid: int) -> Path | None:
    path_buffer = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    path_size = library.proc_pidpath(
        pid,
        path_buffer,
        _PROC_PIDPATHINFO_MAXSIZE,
    )
    if not 1 <= path_size < _PROC_PIDPATHINFO_MAXSIZE:
        return None
    raw_path = bytes(path_buffer.raw[:path_size]).rstrip(b"\x00")
    if not raw_path or b"\x00" in raw_path:
        return None
    executable_path = Path(os.fsdecode(raw_path))
    if not executable_path.is_absolute() or ".." in executable_path.parts:
        return None
    return executable_path


def current_process_identity(pid: int) -> ProcessIdentityEvidence | None:
    """Return immutable macOS process evidence or fail closed."""

    if type(pid) is not int or pid <= 0 or os.uname().sysname != "Darwin":
        return None
    library_name = ctypes.util.find_library("proc")
    if library_name is None:
        return None
    try:
        library = ctypes.CDLL(library_name, use_errno=True)
        library.proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        library.proc_pidinfo.restype = ctypes.c_int
        library.proc_pidpath.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.proc_pidpath.restype = ctypes.c_int
        first_snapshot = _proc_bsd_snapshot(library, pid)
        first_path = _proc_executable_path(library, pid)
        if first_snapshot is None or first_path is None:
            return None
        executable_descriptor = os.open(
            first_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(executable_descriptor)
        finally:
            os.close(executable_descriptor)
        second_snapshot = _proc_bsd_snapshot(library, pid)
        second_path = _proc_executable_path(library, pid)
        if first_snapshot != second_snapshot or first_path != second_path:
            return None
        pathname_metadata = first_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(pathname_metadata.st_mode)
            or metadata.st_dev != pathname_metadata.st_dev
            or metadata.st_ino != pathname_metadata.st_ino
        ):
            return None
        start_seconds, start_microseconds = first_snapshot
        return ProcessIdentityEvidence(
            start_time_ns=start_seconds * 1_000_000_000 + start_microseconds * 1_000,
            executable_path=first_path,
            executable_device=metadata.st_dev,
            executable_inode=metadata.st_ino,
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
