"""Shared macOS runtime authority and exact Connector discovery descriptor v2."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Final, Protocol

RUNTIME_DESCRIPTOR_VERSION: Final = 2
RUNTIME_DESCRIPTOR_FIELDS: Final = frozenset(
    {
        "version",
        "pid",
        "profile",
        "runtime_generation",
        "socket_path",
        "instance_id",
        "process_start_time_ns",
        "process_executable",
        "process_executable_device",
        "process_executable_inode",
        "host_bundle_id",
    }
)

_PROC_PIDTBSDINFO = 3
_PROC_PIDPATHINFO_MAXSIZE = 4096
_MAX_DESCRIPTOR_BYTES = 16_384
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
    def __call__(self, pid: int) -> object | None: ...


@dataclass(frozen=True, slots=True)
class RuntimeEndpointV2:
    authority: MacOSRuntimeAuthorityV2
    socket_path: Path
    socket_device: int
    socket_inode: int
    registry_path: Path

    @property
    def pid(self) -> int:
        return self.authority.pid

    @property
    def profile(self) -> str:
        return self.authority.profile

    @property
    def runtime_generation(self) -> str:
        return self.authority.runtime_generation

    @property
    def instance_id(self) -> str:
        return self.authority.instance_id

    @property
    def host_bundle_id(self) -> str:
        return self.authority.host_bundle_id

    @property
    def process_identity(self) -> ProcessIdentityEvidenceV2:
        return self.authority.process_identity


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


def normalize_process_identity(value: object) -> ProcessIdentityEvidenceV2 | None:
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
    return ProcessIdentityEvidenceV2(
        start_time_ns=start_time_ns,
        executable_path=executable_path,
        executable_device=executable_device,
        executable_inode=executable_inode,
    )


def _proc_bsd_snapshot(library: object, pid: int) -> tuple[int, int] | None:
    bsd = _ProcBSDInfo()
    bsd_size = ctypes.sizeof(bsd)
    if (
        library.proc_pidinfo(  # type: ignore[attr-defined]
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
    path_size = library.proc_pidpath(  # type: ignore[attr-defined]
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


def current_process_identity(pid: int) -> ProcessIdentityEvidenceV2 | None:
    """Capture the same immutable macOS process evidence Connector revalidates."""

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
        return ProcessIdentityEvidenceV2(
            start_time_ns=start_seconds * 1_000_000_000 + start_microseconds * 1_000,
            executable_path=first_path,
            executable_device=metadata.st_dev,
            executable_inode=metadata.st_ino,
        )
    except (OSError, UnicodeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class MacOSHostAuthorityV2:
    pid: int
    profile: str
    instance_id: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidenceV2

    def __post_init__(self) -> None:
        _validate_authority_fields(self)

    def bind_runtime(self, runtime_generation: str) -> MacOSRuntimeAuthorityV2:
        return MacOSRuntimeAuthorityV2(
            pid=self.pid,
            profile=self.profile,
            runtime_generation=_runtime_generation(runtime_generation),
            instance_id=self.instance_id,
            host_bundle_id=self.host_bundle_id,
            process_identity=self.process_identity,
        )


@dataclass(frozen=True, slots=True)
class MacOSRuntimeAuthorityV2:
    pid: int
    profile: str
    runtime_generation: str
    instance_id: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidenceV2

    def __post_init__(self) -> None:
        _validate_authority_fields(self)
        _runtime_generation(self.runtime_generation)


def _validate_authority_fields(value: object) -> None:
    if type(value.pid) is not int or not 1 <= value.pid <= 2_147_483_647:
        raise ValueError("pid must be a valid POSIX pid")
    if not isinstance(value.profile, str) or _PROFILE.fullmatch(value.profile) is None:
        raise ValueError("profile is invalid")
    if (
        not isinstance(value.instance_id, str)
        or _CANONICAL_UUID.fullmatch(value.instance_id) is None
    ):
        raise ValueError("instance_id must be a canonical RFC 4122 UUID")
    try:
        if str(uuid.UUID(value.instance_id)) != value.instance_id:
            raise ValueError("instance_id must be a canonical RFC 4122 UUID")
    except ValueError:
        raise ValueError("instance_id must be a canonical RFC 4122 UUID") from None
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


def capture_macos_host_authority(
    *,
    profile: str,
    host_bundle_id: str,
    pid: int | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
    instance_id_factory: Callable[[], object] = uuid.uuid4,
) -> MacOSHostAuthorityV2:
    process_id = os.getpid() if pid is None else pid
    observed = normalize_process_identity(process_identity_provider(process_id))
    if observed is None:
        raise RuntimeError("process identity unavailable")
    return MacOSHostAuthorityV2(
        pid=process_id,
        profile=profile,
        instance_id=str(instance_id_factory()),
        host_bundle_id=host_bundle_id,
        process_identity=observed,
    )


def require_current_process_authority(
    authority: MacOSRuntimeAuthorityV2,
    *,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> None:
    if not isinstance(authority, MacOSRuntimeAuthorityV2):
        raise TypeError("runtime authority v2 is required")
    observed = normalize_process_identity(process_identity_provider(authority.pid))
    if observed is None:
        raise RuntimeError("process identity unavailable")
    if observed != authority.process_identity:
        raise RuntimeError("process identity mismatch")


def encode_runtime_descriptor_v2(
    authority: MacOSRuntimeAuthorityV2,
    *,
    socket_path: Path,
) -> dict[str, object]:
    if not isinstance(authority, MacOSRuntimeAuthorityV2):
        raise TypeError("runtime authority v2 is required")
    endpoint_path = Path(socket_path)
    endpoint_text = str(endpoint_path)
    if (
        not endpoint_path.is_absolute()
        or ".." in endpoint_path.parts
        or "\x00" in endpoint_text
        or not 2 <= len(endpoint_text) <= _PROC_PIDPATHINFO_MAXSIZE
        or len(os.fsencode(endpoint_path)) > 103
    ):
        raise ValueError("socket_path is invalid")
    process = authority.process_identity
    descriptor: dict[str, object] = {
        "version": RUNTIME_DESCRIPTOR_VERSION,
        "pid": authority.pid,
        "profile": authority.profile,
        "runtime_generation": authority.runtime_generation,
        "socket_path": endpoint_text,
        "instance_id": authority.instance_id,
        "process_start_time_ns": process.start_time_ns,
        "process_executable": str(process.executable_path),
        "process_executable_device": process.executable_device,
        "process_executable_inode": process.executable_inode,
        "host_bundle_id": authority.host_bundle_id,
    }
    assert frozenset(descriptor) == RUNTIME_DESCRIPTOR_FIELDS
    return descriptor


def decode_runtime_descriptor_v2(
    value: object,
    *,
    registry_path: Path,
    socket_directory: Path,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> RuntimeEndpointV2:
    """Decode one exact descriptor around coherent process/socket evidence."""

    if not isinstance(value, dict) or frozenset(value) != RUNTIME_DESCRIPTOR_FIELDS:
        raise ValueError("runtime descriptor fields are invalid")
    if type(value["version"]) is not int or value["version"] != 2:
        raise ValueError("runtime descriptor version is invalid")
    process_identity = normalize_process_identity(
        SimpleNamespace(
            start_time_ns=value["process_start_time_ns"],
            executable_path=value["process_executable"],
            executable_device=value["process_executable_device"],
            executable_inode=value["process_executable_inode"],
        )
    )
    if process_identity is None:
        raise ValueError("runtime descriptor process identity is invalid")
    authority = MacOSRuntimeAuthorityV2(
        pid=value["pid"],
        profile=value["profile"],
        runtime_generation=value["runtime_generation"],
        instance_id=value["instance_id"],
        host_bundle_id=value["host_bundle_id"],
        process_identity=process_identity,
    )
    endpoint_path = Path(value["socket_path"])
    socket_root = Path(socket_directory)
    endpoint_text = str(endpoint_path)
    if (
        not isinstance(value["socket_path"], str)
        or not endpoint_path.is_absolute()
        or ".." in endpoint_path.parts
        or "\x00" in endpoint_text
        or endpoint_path.parent != socket_root
        or not 2 <= len(endpoint_text) <= _PROC_PIDPATHINFO_MAXSIZE
        or len(os.fsencode(endpoint_path)) > 103
    ):
        raise ValueError("runtime descriptor socket path is invalid")
    if (
        normalize_process_identity(process_identity_provider(authority.pid))
        != process_identity
    ):
        raise ValueError("runtime descriptor process identity changed")
    metadata = endpoint_path.lstat()
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise ValueError("runtime descriptor socket is untrusted")
    if (
        normalize_process_identity(process_identity_provider(authority.pid))
        != process_identity
    ):
        raise ValueError("runtime descriptor process identity changed")
    return RuntimeEndpointV2(
        authority=authority,
        socket_path=endpoint_path,
        socket_device=metadata.st_dev,
        socket_inode=metadata.st_ino,
        registry_path=Path(registry_path),
    )


def same_runtime_endpoint_evidence(
    endpoint: RuntimeEndpointV2,
    *,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> bool:
    try:
        metadata = endpoint.socket_path.lstat()
        observed = normalize_process_identity(process_identity_provider(endpoint.pid))
    except BaseException:
        return False
    return (
        _metadata_matches_runtime_socket(endpoint, metadata)
        and observed == endpoint.process_identity
    )


def same_runtime_socket_identity(endpoint: RuntimeEndpointV2) -> bool:
    try:
        metadata = endpoint.socket_path.lstat()
    except OSError:
        return False
    return _metadata_matches_runtime_socket(endpoint, metadata)


def _metadata_matches_runtime_socket(
    endpoint: RuntimeEndpointV2,
    metadata: os.stat_result,
) -> bool:
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.geteuid()
        and metadata.st_dev == endpoint.socket_device
        and metadata.st_ino == endpoint.socket_inode
    )


def publish_runtime_descriptor_v2(
    *,
    registry_directory: Path,
    target: Path,
    authority: MacOSRuntimeAuthorityV2,
    socket_path: Path,
) -> None:
    registry = Path(registry_directory)
    destination = Path(target)
    if destination.parent != registry:
        raise ValueError("descriptor target must be inside registry directory")
    payload = json.dumps(
        encode_runtime_descriptor_v2(authority, socket_path=socket_path),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not 1 <= len(payload) <= _MAX_DESCRIPTOR_BYTES:
        raise ValueError("descriptor size is outside limits")
    registry_metadata = registry.lstat()
    if (
        not stat.S_ISDIR(registry_metadata.st_mode)
        or stat.S_IMODE(registry_metadata.st_mode) != 0o700
        or registry_metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("descriptor registry directory is not private")

    directory_descriptor = os.open(
        registry,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        opened_registry_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(opened_registry_metadata.st_mode)
            or stat.S_IMODE(opened_registry_metadata.st_mode) != 0o700
            or opened_registry_metadata.st_uid != os.geteuid()
            or opened_registry_metadata.st_dev != registry_metadata.st_dev
            or opened_registry_metadata.st_ino != registry_metadata.st_ino
        ):
            raise RuntimeError("descriptor registry directory changed while opening")

        temporary_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short descriptor write")
            offset += written
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise RuntimeError("descriptor temporary file is not private")
        os.close(descriptor)
        descriptor = None
        os.rename(
            temporary_name,
            destination.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        raise
    finally:
        os.close(directory_descriptor)


__all__ = [
    "RUNTIME_DESCRIPTOR_FIELDS",
    "RUNTIME_DESCRIPTOR_VERSION",
    "MacOSHostAuthorityV2",
    "MacOSRuntimeAuthorityV2",
    "ProcessIdentityEvidenceV2",
    "RuntimeEndpointV2",
    "capture_macos_host_authority",
    "current_process_identity",
    "decode_runtime_descriptor_v2",
    "encode_runtime_descriptor_v2",
    "normalize_process_identity",
    "publish_runtime_descriptor_v2",
    "require_current_process_authority",
    "same_runtime_endpoint_evidence",
    "same_runtime_socket_identity",
]
