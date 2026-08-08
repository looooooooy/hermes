"""Private atomic macOS Connector readiness receipt persistence."""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from hermes_connector.adapters.platform.macos.instance_identity import (
    _fsync_directory,
    _write_all,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
)
from hermes_connector.adapters.status_receipt_codec import (
    MAX_STATUS_RECEIPT_BYTES,
    decode_status_receipt,
    encode_status_receipt,
    normalize_process_identity_evidence,
    timestamp_is_current,
)
from hermes_connector.domain.readiness_status import ConnectorStatusReceipt

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
            if not timestamp_is_current(receipt.updated_at, now):
                return None
            observed = normalize_process_identity_evidence(
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


def _validate_receipt_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= MAX_STATUS_RECEIPT_BYTES
    ):
        raise UnsafeStatusReceipt("status receipt metadata is unsafe")


def _read_bounded(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_STATUS_RECEIPT_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size or not 1 <= len(raw) <= MAX_STATUS_RECEIPT_BYTES:
        raise UnsafeStatusReceipt("status receipt changed during read")
    return raw


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


__all__ = [
    "MacOSStatusReceiptStore",
    "UnsafeStatusReceipt",
    "decode_status_receipt",
    "encode_status_receipt",
]
