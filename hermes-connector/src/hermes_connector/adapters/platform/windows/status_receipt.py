"""Private atomic Windows Connector readiness receipt persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from hermes_connector.adapters.status_receipt_codec import (
    MAX_STATUS_RECEIPT_BYTES,
    decode_status_receipt,
    encode_status_receipt,
    normalize_process_identity_evidence,
    timestamp_is_current,
)
from hermes_connector.domain.local_gateway import ProcessIdentityEvidence
from hermes_connector.domain.readiness_status import ConnectorStatusReceipt

from .private_state import (
    UnsafeWindowsPrivateState,
    atomic_write_private_file,
    delete_private_file,
    read_private_file,
    validate_private_directory,
    validate_private_file,
)

ProcessIdentityProvider = Callable[[int], ProcessIdentityEvidence | None]


class UnsafeStatusReceipt(ValueError):
    """The Windows receipt reference or private-state metadata is unsafe."""


class WindowsStatusReceiptStore:
    """Publish, validate, and remove one bounded current-user status receipt."""

    __slots__ = ("_path",)

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def publish(self, receipt: ConnectorStatusReceipt) -> None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            if self._path.exists():
                validate_private_file(self._path)
            atomic_write_private_file(
                self._path,
                encode_status_receipt(receipt),
                maximum=MAX_STATUS_RECEIPT_BYTES,
            )
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState) as error:
            raise UnsafeStatusReceipt("status receipt could not be published") from error

    def read(
        self,
        *,
        now: datetime,
        process_identity_provider: ProcessIdentityProvider,
    ) -> ConnectorStatusReceipt | None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            raw = read_private_file(
                self._path,
                maximum=MAX_STATUS_RECEIPT_BYTES,
            )
            if raw is None:
                return None
            receipt = decode_status_receipt(raw)
            if not timestamp_is_current(receipt.updated_at, now):
                return None
            observed = normalize_process_identity_evidence(
                process_identity_provider(receipt.pid)
            )
            if observed != receipt.process_identity:
                return None
            return receipt
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            UnicodeError,
            UnsafeWindowsPrivateState,
        ):
            return None

    def remove(self) -> None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            delete_private_file(self._path)
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState) as error:
            raise UnsafeStatusReceipt("status receipt could not be removed") from error

    def _validate_reference(self) -> None:
        if (
            not self._path.is_absolute()
            or "\x00" in str(self._path)
            or self._path.name in {"", ".", ".."}
            or ".." in self._path.parts
        ):
            raise UnsafeStatusReceipt("status receipt reference is unsafe")


__all__ = ["UnsafeStatusReceipt", "WindowsStatusReceiptStore"]
