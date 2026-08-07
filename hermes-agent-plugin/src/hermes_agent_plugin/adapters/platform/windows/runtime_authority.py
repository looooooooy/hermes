from __future__ import annotations

import ctypes
import os
import re
import stat
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH_CHARS = 32768
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@dataclass(frozen=True, slots=True)
class ProcessIdentityEvidenceV2:
    start_time_ns: int
    executable_path: Path
    executable_device: int
    executable_inode: int


class ProcessIdentityProvider(Protocol):
    def __call__(self, pid: int) -> ProcessIdentityEvidenceV2 | None: ...


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


def _ticks(value: _FileTime) -> int:
    return (int(value.high) << 32) | int(value.low)


def _snapshot(pid: int) -> tuple[int, Path] | None:
    kernel32 = _kernel32()
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
        start = _ticks(created)
        if start <= 0:
            return None
        path_buffer = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
        length = wintypes.DWORD(len(path_buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            path_buffer,
            ctypes.byref(length),
        ):
            return None
        if not 1 <= length.value < len(path_buffer):
            return None
        executable = Path(path_buffer.value)
        if not executable.is_absolute() or ".." in executable.parts:
            return None
        return start * 100, executable
    finally:
        kernel32.CloseHandle(handle)


def current_process_identity(pid: int) -> ProcessIdentityEvidenceV2 | None:
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    try:
        first = _snapshot(pid)
        if first is None:
            return None
        start_time_ns, executable = first
        metadata = executable.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_ino <= 0:
            return None
        if _snapshot(pid) != first:
            return None
        return ProcessIdentityEvidenceV2(
            start_time_ns=start_time_ns,
            executable_path=executable,
            executable_device=int(metadata.st_dev),
            executable_inode=int(metadata.st_ino),
        )
    except (OSError, UnicodeError, ValueError):
        return None


def normalize_process_identity(value: object) -> ProcessIdentityEvidenceV2 | None:
    try:
        result = ProcessIdentityEvidenceV2(
            start_time_ns=value.start_time_ns,
            executable_path=Path(value.executable_path),
            executable_device=value.executable_device,
            executable_inode=value.executable_inode,
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        type(result.start_time_ns) is not int
        or result.start_time_ns <= 0
        or type(result.executable_device) is not int
        or result.executable_device < 0
        or type(result.executable_inode) is not int
        or result.executable_inode <= 0
        or not result.executable_path.is_absolute()
        or ".." in result.executable_path.parts
        or "\x00" in str(result.executable_path)
    ):
        return None
    return result


@dataclass(frozen=True, slots=True)
class WindowsHostAuthorityV2:
    pid: int
    profile: str
    instance_id: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidenceV2

    def __post_init__(self) -> None:
        _validate_authority(self)

    def bind_runtime(self, runtime_generation: str) -> WindowsRuntimeAuthorityV2:
        return WindowsRuntimeAuthorityV2(
            pid=self.pid,
            profile=self.profile,
            runtime_generation=_runtime_generation(runtime_generation),
            instance_id=self.instance_id,
            host_bundle_id=self.host_bundle_id,
            process_identity=self.process_identity,
        )


@dataclass(frozen=True, slots=True)
class WindowsRuntimeAuthorityV2:
    pid: int
    profile: str
    runtime_generation: str
    instance_id: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidenceV2

    def __post_init__(self) -> None:
        _validate_authority(self)
        _runtime_generation(self.runtime_generation)


def _validate_authority(value: object) -> None:
    if type(value.pid) is not int or not 1 <= value.pid <= 2_147_483_647:
        raise ValueError("pid is invalid")
    if not isinstance(value.profile, str) or _PROFILE.fullmatch(value.profile) is None:
        raise ValueError("profile is invalid")
    if (
        not isinstance(value.instance_id, str)
        or _CANONICAL_UUID.fullmatch(value.instance_id) is None
        or str(uuid.UUID(value.instance_id)) != value.instance_id
    ):
        raise ValueError("instance_id must be a canonical RFC 4122 UUID")
    if (
        not isinstance(value.host_bundle_id, str)
        or _BUNDLE_ID.fullmatch(value.host_bundle_id) is None
    ):
        raise ValueError("host_bundle_id is invalid")
    if normalize_process_identity(value.process_identity) != value.process_identity:
        raise ValueError("process identity is invalid")


def _runtime_generation(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError("runtime_generation is invalid")
    return value


def capture_windows_host_authority(
    *,
    profile: str,
    host_bundle_id: str,
    pid: int | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
    instance_id_factory: Callable[[], object] = uuid.uuid4,
) -> WindowsHostAuthorityV2:
    endpoint_pid = os.getpid() if pid is None else pid
    process_identity = process_identity_provider(endpoint_pid)
    if process_identity is None:
        raise RuntimeError("Windows Host process identity is unavailable")
    instance_id = str(instance_id_factory())
    return WindowsHostAuthorityV2(
        pid=endpoint_pid,
        profile=profile,
        instance_id=instance_id,
        host_bundle_id=host_bundle_id,
        process_identity=process_identity,
    )


def require_current_process_authority(
    authority: WindowsRuntimeAuthorityV2,
    *,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> None:
    if authority.pid != os.getpid():
        raise RuntimeError("runtime authority pid does not match current process")
    observed = process_identity_provider(authority.pid)
    if normalize_process_identity(observed) != authority.process_identity:
        raise RuntimeError("runtime authority process identity changed")


__all__ = [
    "ProcessIdentityEvidenceV2",
    "ProcessIdentityProvider",
    "WindowsHostAuthorityV2",
    "WindowsRuntimeAuthorityV2",
    "capture_windows_host_authority",
    "current_process_identity",
    "normalize_process_identity",
    "require_current_process_authority",
]
