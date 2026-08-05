"""Private atomic macOS Connector readiness receipt persistence."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hermes_connector.adapters.platform.macos.instance_identity import (
    _fsync_directory,
    _write_all,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    normalize_process_identity,
)
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import ProcessIdentityEvidence
from hermes_connector.domain.readiness_status import (
    LOCAL_AUTHORITY_IDENTITY_FIELDS,
    STATUS_RECEIPT_FIELDS,
    STATUS_RECEIPT_FUTURE_SKEW_SECONDS,
    STATUS_RECEIPT_TTL_SECONDS,
    ConnectorStatusReceipt,
    LocalAuthorityIdentity,
    validate_release_id,
)

_MAX_RECEIPT_BYTES = 8_192
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class UnsafeStatusReceipt(ValueError):
    """The receipt reference or filesystem metadata is unsafe."""


class MacOSStatusReceiptStore:
    """Publish, validate, and remove one bounded private status receipt."""

    __slots__ = ("_path",)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def publish(self, receipt: ConnectorStatusReceipt) -> None:
        self._validate_reference()
        self._validate_parent()
        raw = encode_status_receipt(receipt)
        self._validate_existing_target()
        temporary = self._path.parent / (
            f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, _WRITE_FLAGS, 0o600)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def read(
        self,
        *,
        now: datetime,
        process_identity_provider: ProcessIdentityProvider,
    ) -> ConnectorStatusReceipt | None:
        try:
            self._validate_reference()
            self._validate_parent()
            before = self._path.lstat()
            _validate_receipt_metadata(before)
            descriptor = os.open(self._path, _READ_FLAGS)
            try:
                opened = os.fstat(descriptor)
                _validate_receipt_metadata(opened)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return None
                raw = _read_bounded(descriptor, opened.st_size)
                after = os.fstat(descriptor)
                if _metadata_identity(opened) != _metadata_identity(after):
                    return None
            finally:
                os.close(descriptor)
            receipt = decode_status_receipt(raw)
            if not _timestamp_is_current(receipt.updated_at, now):
                return None
            observed = normalize_process_identity(
                process_identity_provider(receipt.pid)
            )
            if observed != receipt.process_identity:
                return None
            return receipt
        except (OSError, TypeError, ValueError, UnicodeError):
            return None

    def remove(self) -> None:
        self._validate_reference()
        self._validate_parent()
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return
        _validate_receipt_metadata(metadata)
        self._path.unlink()
        _fsync_directory(self._path.parent)

    def _validate_reference(self) -> None:
        if (
            not self._path.is_absolute()
            or "\x00" in str(self._path)
            or self._path.name in {"", ".", ".."}
            or ".." in self._path.parts
        ):
            raise UnsafeStatusReceipt("status receipt reference is unsafe")

    def _validate_parent(self) -> None:
        metadata = self._path.parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise UnsafeStatusReceipt("status receipt directory is unsafe")

    def _validate_existing_target(self) -> None:
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return
        _validate_receipt_metadata(metadata)


def encode_status_receipt(receipt: ConnectorStatusReceipt) -> bytes:
    value = _receipt_payload(receipt)
    raw = json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if not 1 <= len(raw) <= _MAX_RECEIPT_BYTES:
        raise ValueError("status receipt is outside the size limit")
    return raw


def decode_status_receipt(raw: bytes) -> ConnectorStatusReceipt:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("status receipt is invalid") from None
    if not isinstance(value, dict) or frozenset(value) != STATUS_RECEIPT_FIELDS:
        raise ValueError("status receipt fields are invalid")
    authority = value["local_authority_identity"]
    if (
        not isinstance(authority, dict)
        or frozenset(authority) != LOCAL_AUTHORITY_IDENTITY_FIELDS
    ):
        raise ValueError("local authority identity fields are invalid")

    release_id = validate_release_id(value["release_id"])
    pid = value["pid"]
    process_start_time_ns = value["process_start_time_ns"]
    executable = value["process_executable"]
    executable_device = value["process_executable_device"]
    executable_inode = value["process_executable_inode"]
    ready = value["ready"]
    if (
        type(pid) is not int
        or not 1 <= pid <= 2_147_483_647
        or type(process_start_time_ns) is not int
        or process_start_time_ns <= 0
        or not isinstance(executable, str)
        or not 2 <= len(executable) <= 4096
        or "\x00" in executable
        or type(executable_device) is not int
        or executable_device < 0
        or type(executable_inode) is not int
        or executable_inode <= 0
        or type(ready) is not bool
    ):
        raise ValueError("status receipt process fields are invalid")
    executable_path = Path(executable)
    if not executable_path.is_absolute() or ".." in executable_path.parts:
        raise ValueError("status receipt executable is invalid")

    runtime_generation = _bounded_identity(
        value["runtime_generation"], "runtime generation", maximum=128
    )
    profile = _bounded_identity(authority["profile"], "profile", maximum=128)
    instance_id = _canonical_uuid(authority["instance_id"])
    host_bundle_id = _bounded_identity(
        authority["host_bundle_id"], "host bundle id", maximum=255
    )
    try:
        cloud_state = CloudSessionState(value["cloud_state"])
    except (TypeError, ValueError):
        raise ValueError("status receipt cloud state is invalid") from None
    if ready and cloud_state is not CloudSessionState.ACTIVE:
        raise ValueError("ready status receipt must have active Cloud state")
    updated_at = _parse_datetime(value["updated_at"])
    process_identity = normalize_process_identity(
        ProcessIdentityEvidence(
            start_time_ns=process_start_time_ns,
            executable_path=executable_path,
            executable_device=executable_device,
            executable_inode=executable_inode,
        )
    )
    if process_identity is None:
        raise ValueError("status receipt process identity is invalid")
    return ConnectorStatusReceipt(
        release_id=release_id,
        pid=pid,
        process_identity=process_identity,
        runtime_generation=runtime_generation,
        local_authority_identity=LocalAuthorityIdentity(
            profile=profile,
            instance_id=instance_id,
            host_bundle_id=host_bundle_id,
        ),
        cloud_state=cloud_state,
        updated_at=updated_at,
        ready=ready,
    )


def _receipt_payload(receipt: ConnectorStatusReceipt) -> dict[str, object]:
    validated = decode_status_receipt(
        json.dumps(
            {
                "cloud_state": receipt.cloud_state.value,
                "local_authority_identity": {
                    "host_bundle_id": receipt.local_authority_identity.host_bundle_id,
                    "instance_id": receipt.local_authority_identity.instance_id,
                    "profile": receipt.local_authority_identity.profile,
                },
                "pid": receipt.pid,
                "process_executable": str(receipt.process_identity.executable_path),
                "process_executable_device": (
                    receipt.process_identity.executable_device
                ),
                "process_executable_inode": receipt.process_identity.executable_inode,
                "process_start_time_ns": receipt.process_identity.start_time_ns,
                "ready": receipt.ready,
                "release_id": receipt.release_id,
                "runtime_generation": receipt.runtime_generation,
                "updated_at": _format_datetime(receipt.updated_at),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return {
        "cloud_state": validated.cloud_state.value,
        "local_authority_identity": {
            "host_bundle_id": validated.local_authority_identity.host_bundle_id,
            "instance_id": validated.local_authority_identity.instance_id,
            "profile": validated.local_authority_identity.profile,
        },
        "pid": validated.pid,
        "process_executable": str(validated.process_identity.executable_path),
        "process_executable_device": validated.process_identity.executable_device,
        "process_executable_inode": validated.process_identity.executable_inode,
        "process_start_time_ns": validated.process_identity.start_time_ns,
        "ready": validated.ready,
        "release_id": validated.release_id,
        "runtime_generation": validated.runtime_generation,
        "updated_at": _format_datetime(validated.updated_at),
    }


def _validate_receipt_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= _MAX_RECEIPT_BYTES
    ):
        raise UnsafeStatusReceipt("status receipt metadata is unsafe")


def _read_bounded(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_RECEIPT_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size or not 1 <= len(raw) <= _MAX_RECEIPT_BYTES:
        raise UnsafeStatusReceipt("status receipt changed during read")
    return raw


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("status receipt contains duplicate fields")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("status receipt contains a non-JSON number")


def _bounded_identity(value: object, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"status receipt {label} is invalid")
    return value


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("status receipt instance id is invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("status receipt instance id is invalid") from None
    if str(parsed) != value:
        raise ValueError("status receipt instance id is invalid")
    return value


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("status receipt timestamp must be timezone-aware")
    utc = value.astimezone(UTC)
    if utc.microsecond:
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("status receipt timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError("status receipt timestamp is invalid") from None
    if _format_datetime(parsed) != value:
        raise ValueError("status receipt timestamp is not canonical")
    return parsed


def _timestamp_is_current(updated_at: datetime, now: datetime) -> bool:
    if now.tzinfo is None:
        return False
    age = (now.astimezone(UTC) - updated_at.astimezone(UTC)).total_seconds()
    return -STATUS_RECEIPT_FUTURE_SKEW_SECONDS <= age <= STATUS_RECEIPT_TTL_SECONDS
