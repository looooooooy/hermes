from __future__ import annotations

import ctypes
import hashlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

from .named_pipe import close_handle, current_user_sid_string

_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_FILE_ALL_ACCESS = 0x001F01FF
_GENERIC_ALL = 0x10000000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_PATH_CHARS = 32_767


class UnsafeWindowsPrivateState(ValueError):
    """A private state path, ACL, or bounded file operation is unsafe."""


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


_LIBRARIES: tuple[object, object] | None = None


def _libraries() -> tuple[object, object]:
    global _LIBRARIES
    if os.name != "nt":
        raise RuntimeError("Windows private state requires Windows")
    if _LIBRARIES is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _configure(kernel32, advapi32)
        _LIBRARIES = kernel32, advapi32
    return _LIBRARIES


def _configure(kernel32: object, advapi32: object) -> None:
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
    void_pointer_pointer = ctypes.POINTER(ctypes.c_void_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    kernel32.GetFileSizeEx.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
    kernel32.DeleteFileW.restype = wintypes.BOOL
    kernel32.CreateMutexW.argtypes = [
        ctypes.POINTER(_SecurityAttributes),
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        void_pointer_pointer,
        dword_pointer,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        void_pointer_pointer,
        void_pointer_pointer,
        void_pointer_pointer,
        void_pointer_pointer,
        void_pointer_pointer,
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, void_pointer_pointer]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL


def _validate_path(path: str | os.PathLike[str]) -> Path:
    value = Path(path)
    text = str(value)
    if (
        not value.is_absolute()
        or ".." in value.parts
        or "\x00" in text
        or len(text) > _MAX_PATH_CHARS
        or value.name in {"", ".", ".."}
    ):
        raise UnsafeWindowsPrivateState("Windows private state path is unsafe")
    return value


@contextmanager
def _security_attributes() -> Iterator[_SecurityAttributes]:
    kernel32, advapi32 = _libraries()
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sid = current_user_sid_string()
    sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise OSError(
            ctypes.get_last_error(),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
        )
    if not descriptor.value or descriptor_size.value == 0:
        if descriptor.value:
            kernel32.LocalFree(descriptor)
        raise UnsafeWindowsPrivateState("Windows security descriptor is empty")
    attributes = _SecurityAttributes(
        nLength=ctypes.sizeof(_SecurityAttributes),
        lpSecurityDescriptor=descriptor.value,
        bInheritHandle=False,
    )
    try:
        yield attributes
    finally:
        kernel32.LocalFree(descriptor)


def ensure_private_directory(path: str | os.PathLike[str]) -> Path:
    target = _validate_path(path)
    kernel32, _ = _libraries()
    with _security_attributes() as attributes:
        created = kernel32.CreateDirectoryW(str(target), ctypes.byref(attributes))
    if not created:
        error = ctypes.get_last_error()
        if error != _ERROR_ALREADY_EXISTS:
            raise OSError(error, "CreateDirectoryW failed")
    validate_private_directory(target)
    return target


def validate_private_directory(path: str | os.PathLike[str]) -> None:
    target = _validate_path(path)
    attributes = _file_attributes(target)
    if (
        attributes & _FILE_ATTRIBUTE_DIRECTORY == 0
        or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise UnsafeWindowsPrivateState("Windows private directory type is unsafe")
    _validate_security(target)


def validate_private_file(path: str | os.PathLike[str]) -> None:
    target = _validate_path(path)
    attributes = _file_attributes(target)
    if attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT):
        raise UnsafeWindowsPrivateState("Windows private file type is unsafe")
    _validate_security(target)


def private_file_exists(path: str | os.PathLike[str]) -> bool:
    target = _validate_path(path)
    kernel32, _ = _libraries()
    ctypes.set_last_error(0)
    value = int(kernel32.GetFileAttributesW(str(target)))
    if value != _INVALID_FILE_ATTRIBUTES:
        return True
    error = ctypes.get_last_error()
    if error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
        return False
    raise OSError(error, "GetFileAttributesW failed")


def read_private_file(
    path: str | os.PathLike[str],
    *,
    maximum: int,
) -> bytes | None:
    target = _validate_path(path)
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("private file maximum must be positive")
    if not private_file_exists(target):
        return None
    validate_private_file(target)
    kernel32, _ = _libraries()
    handle = kernel32.CreateFileW(
        str(target),
        _GENERIC_READ,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), "CreateFileW read failed")
    raw_handle = int(handle)
    try:
        size = ctypes.c_longlong()
        if not kernel32.GetFileSizeEx(wintypes.HANDLE(raw_handle), ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetFileSizeEx failed")
        if not 1 <= size.value <= maximum:
            raise UnsafeWindowsPrivateState("Windows private file size is unsafe")
        return _read_exact(raw_handle, int(size.value))
    finally:
        close_handle(raw_handle)


def atomic_write_private_file(
    path: str | os.PathLike[str],
    raw: bytes,
    *,
    maximum: int,
) -> None:
    target = _validate_path(path)
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= maximum:
        raise UnsafeWindowsPrivateState("Windows private file payload is unsafe")
    validate_private_directory(target.parent)
    if private_file_exists(target):
        validate_private_file(target)
    temporary = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
    kernel32, _ = _libraries()
    handle: int | None = None
    try:
        with _security_attributes() as attributes:
            created = kernel32.CreateFileW(
                str(temporary),
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                ctypes.byref(attributes),
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
        if created in (None, 0, _INVALID_HANDLE_VALUE):
            raise OSError(ctypes.get_last_error(), "CreateFileW write failed")
        handle = int(created)
        _write_all(handle, raw)
        if not kernel32.FlushFileBuffers(wintypes.HANDLE(handle)):
            raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")
        close_handle(handle)
        handle = None
        if not kernel32.MoveFileExW(
            str(temporary),
            str(target),
            _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
        ):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        validate_private_file(target)
    finally:
        if handle is not None:
            close_handle(handle)
        if private_file_exists(temporary):
            kernel32.DeleteFileW(str(temporary))


def delete_private_file(path: str | os.PathLike[str]) -> bool:
    target = _validate_path(path)
    if not private_file_exists(target):
        return False
    validate_private_file(target)
    kernel32, _ = _libraries()
    if not kernel32.DeleteFileW(str(target)):
        error = ctypes.get_last_error()
        if error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            return False
        raise OSError(error, "DeleteFileW failed")
    return True


@contextmanager
def private_named_mutex(
    key: str,
    *,
    timeout_seconds: float = 5.0,
) -> Iterator[None]:
    if (
        not isinstance(key, str)
        or not key
        or key != key.strip()
        or "\x00" in key
        or timeout_seconds <= 0
    ):
        raise ValueError("Windows private mutex input is invalid")
    sid = current_user_sid_string()
    digest = hashlib.sha256(f"{sid}\0{key}".encode("utf-8")).hexdigest()[:40]
    name = f"Local\\HermesConnectorState-{digest}"
    kernel32, _ = _libraries()
    with _security_attributes() as attributes:
        raw = kernel32.CreateMutexW(ctypes.byref(attributes), False, name)
    if raw in (None, 0, _INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    handle = int(raw)
    acquired = False
    try:
        timeout_ms = min(0xFFFFFFFE, max(1, int(timeout_seconds * 1000)))
        result = int(kernel32.WaitForSingleObject(wintypes.HANDLE(handle), timeout_ms))
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            if result == _WAIT_TIMEOUT:
                raise TimeoutError("Windows private state mutex timed out")
            raise OSError(result, "WaitForSingleObject failed")
        acquired = True
        yield
    finally:
        if acquired and not kernel32.ReleaseMutex(wintypes.HANDLE(handle)):
            release_error = ctypes.get_last_error()
            close_handle(handle)
            raise OSError(release_error, "ReleaseMutex failed")
        close_handle(handle)


def _file_attributes(path: Path) -> int:
    kernel32, _ = _libraries()
    ctypes.set_last_error(0)
    value = int(kernel32.GetFileAttributesW(str(path)))
    if value == _INVALID_FILE_ATTRIBUTES:
        raise OSError(ctypes.get_last_error(), "GetFileAttributesW failed")
    return value


def _validate_security(path: Path) -> None:
    kernel32, advapi32 = _libraries()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = int(
        advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result != 0:
        raise OSError(result, "GetNamedSecurityInfoW failed")
    try:
        if not owner.value or not dacl.value or not descriptor.value:
            raise UnsafeWindowsPrivateState("Windows private ACL is incomplete")
        sid = current_user_sid_string()
        if _sid_string(owner.value) != sid:
            raise UnsafeWindowsPrivateState("Windows private state owner changed")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise OSError(ctypes.get_last_error(), "GetSecurityDescriptorControl failed")
        if not control.value & _SE_DACL_PROTECTED:
            raise UnsafeWindowsPrivateState("Windows private DACL is not protected")
        info = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise OSError(ctypes.get_last_error(), "GetAclInformation failed")
        if info.AceCount != 1:
            raise UnsafeWindowsPrivateState("Windows private DACL has extra ACEs")
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            raise OSError(ctypes.get_last_error(), "GetAce failed")
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
        if ace.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE or ace.Header.AceFlags != 0:
            raise UnsafeWindowsPrivateState("Windows private ACE type is unsafe")
        if ace.Mask not in {_FILE_ALL_ACCESS, _GENERIC_ALL}:
            raise UnsafeWindowsPrivateState("Windows private ACE rights are unsafe")
        sid_pointer = ace_pointer.value + _AccessAllowedAce.SidStart.offset
        if _sid_string(sid_pointer) != sid:
            raise UnsafeWindowsPrivateState("Windows private ACE SID changed")
    finally:
        kernel32.LocalFree(descriptor)


def _sid_string(pointer: int) -> str:
    kernel32, advapi32 = _libraries()
    output = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(
        ctypes.c_void_p(pointer),
        ctypes.byref(output),
    ):
        raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
    try:
        value = output.value
        if not value or len(value) > 256:
            raise UnsafeWindowsPrivateState("Windows SID is invalid")
        return value
    finally:
        kernel32.LocalFree(ctypes.cast(output, ctypes.c_void_p))


def _read_exact(handle: int, size: int) -> bytes:
    kernel32, _ = _libraries()
    result = bytearray()
    while len(result) < size:
        chunk_size = min(size - len(result), 4096)
        buffer = ctypes.create_string_buffer(chunk_size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            chunk_size,
            ctypes.byref(read),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ReadFile failed")
        if read.value == 0:
            raise UnsafeWindowsPrivateState("Windows private file ended early")
        result.extend(buffer.raw[: read.value])
    return bytes(result)


def _write_all(handle: int, raw: bytes) -> None:
    kernel32, _ = _libraries()
    offset = 0
    while offset < len(raw):
        buffer = ctypes.create_string_buffer(raw[offset:])
        written = wintypes.DWORD()
        if not kernel32.WriteFile(
            wintypes.HANDLE(handle),
            buffer,
            len(raw) - offset,
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if written.value == 0:
            raise UnsafeWindowsPrivateState("Windows private file write stalled")
        offset += written.value


__all__ = [
    "UnsafeWindowsPrivateState",
    "atomic_write_private_file",
    "delete_private_file",
    "ensure_private_directory",
    "private_file_exists",
    "private_named_mutex",
    "read_private_file",
    "validate_private_directory",
    "validate_private_file",
]
