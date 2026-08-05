"""Thread-safe explicit controller leases.

This module is deliberately transport- and RPC-agnostic.  The authoritative
Gateway owns one manager and adapts authenticated control transports to the
binding objects below.  Lease material is never included in reprs or errors.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ControlLeaseError(Exception):
    """Base class for display-safe control lease failures."""


class ControllerConflict(ControlLeaseError):
    """Another explicit controller owns the target session lease."""


class LeaseRequired(ControlLeaseError):
    """No explicit controller lease exists for the target session."""


class LeaseExpired(ControlLeaseError):
    """The target session lease expired before this operation."""


class LeaseMismatch(ControlLeaseError):
    """Lease material or its authenticated binding does not match."""


class SessionBindingMismatch(ControlLeaseError):
    """Durable session, profile, or runtime binding does not match."""


@dataclass(frozen=True)
class ControlBinding:
    session_key: str
    profile: str
    runtime_generation: str
    runtime_session_id: str | None
    user_id: str
    provider: str
    client_instance_id: str
    transport_id: str

    def __post_init__(self) -> None:
        required = {
            "session_key": self.session_key,
            "profile": self.profile,
            "runtime_generation": self.runtime_generation,
            "user_id": self.user_id,
            "provider": self.provider,
            "client_instance_id": self.client_instance_id,
            "transport_id": self.transport_id,
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in required.values()
        ):
            raise ValueError("control binding fields must be non-empty strings")
        if self.runtime_session_id is not None and not self.runtime_session_id.strip():
            raise ValueError("runtime_session_id must be null or non-empty")

    @property
    def session_identity(self) -> tuple[str, str]:
        return self.profile, self.session_key


@dataclass(frozen=True, repr=False)
class ControlLease:
    lease_id: str
    expires_at_epoch_ms: int
    control_revision: int
    controller_kind: str = "mobile"
    controller_label: str = "Hermes Mobile"
    pending_input: None = None

    def __repr__(self) -> str:
        return (
            "ControlLease(lease_id=[REDACTED], "
            f"expires_at_epoch_ms={self.expires_at_epoch_ms}, "
            f"control_revision={self.control_revision}, "
            f"controller_kind={self.controller_kind!r}, "
            f"controller_label={self.controller_label!r}, pending_input=None)"
        )

    __str__ = __repr__

    def result(self) -> dict[str, Any]:
        """Return the authenticated control RPC result, including lease material."""
        return {
            "lease_id": self.lease_id,
            "expires_at_epoch_ms": self.expires_at_epoch_ms,
            "control_revision": self.control_revision,
            "controller_kind": self.controller_kind,
            "controller_label": self.controller_label,
            "pending_input": None,
        }


@dataclass
class _LeaseRecord:
    lease: ControlLease
    binding: ControlBinding
    expires_at_monotonic: float
    disconnected_at_monotonic: float | None = None


class ControlLeaseManager:
    """Own explicit controller leases without changing an Agent owner transport."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        reconnect_grace_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        epoch_time: Callable[[], float] = time.time,
        lease_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if reconnect_grace_seconds < 0:
            raise ValueError("reconnect_grace_seconds must not be negative")
        self._ttl_seconds = float(ttl_seconds)
        self._reconnect_grace_seconds = float(reconnect_grace_seconds)
        self._monotonic = monotonic
        self._epoch_time = epoch_time
        self._lease_id_factory = lease_id_factory or (lambda: secrets.token_urlsafe(32))
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], _LeaseRecord] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._runtime_identity: tuple[str, str] | None = None
        self._runtime_ready = False

    def bind_runtime(
        self,
        *,
        profile: str,
        runtime_generation: str,
        ready: bool,
    ) -> None:
        """Install the authoritative generation fence and revoke older leases."""
        if (
            not isinstance(profile, str)
            or not profile.strip()
            or profile != profile.strip()
            or not isinstance(runtime_generation, str)
            or not runtime_generation.strip()
            or runtime_generation != runtime_generation.strip()
            or type(ready) is not bool
        ):
            raise ValueError("runtime binding is invalid")
        identity = profile, runtime_generation
        with self._lock:
            if self._runtime_identity == identity and self._runtime_ready == ready:
                return
            self._runtime_identity = identity
            self._runtime_ready = ready
            self._revoke_all_locked()

    def deactivate_runtime(self) -> None:
        """Fail closed after the authoritative Host extension is closed."""
        with self._lock:
            self._runtime_ready = False
            self._revoke_all_locked()

    def acquire(self, binding: ControlBinding) -> ControlLease:
        with self._lock:
            self._require_current_runtime(binding)
            key = binding.session_identity
            existing = self._active_record(key)
            if existing is not None:
                if existing.binding == binding:
                    return existing.lease
                if self._may_rebind(existing, binding):
                    return self._mint(binding)
                raise ControllerConflict("another explicit controller holds the lease")
            return self._mint(binding)

    def renew(self, binding: ControlBinding, *, lease_id: str) -> ControlLease:
        with self._lock:
            self._require_current_runtime(binding)
            record = self._required_record(binding.session_identity, expired_error=True)
            self._require_exact_binding(record, binding, lease_id)
            revision = self._next_revision(binding.session_identity)
            now_monotonic = self._monotonic()
            now_epoch = self._epoch_time()
            lease = ControlLease(
                lease_id=record.lease.lease_id,
                expires_at_epoch_ms=int((now_epoch + self._ttl_seconds) * 1000),
                control_revision=revision,
            )
            record.lease = lease
            record.expires_at_monotonic = now_monotonic + self._ttl_seconds
            record.disconnected_at_monotonic = None
            return lease

    def release(self, binding: ControlBinding, *, lease_id: str) -> dict[str, Any]:
        with self._lock:
            self._require_current_runtime(binding)
            key = binding.session_identity
            record = self._required_record(key, expired_error=True)
            self._require_exact_binding(record, binding, lease_id)
            self._records.pop(key, None)
            revision = self._next_revision(key)
            return {"released": True, "control_revision": revision}

    def authorize(self, binding: ControlBinding, *, lease_id: str) -> ControlLease:
        with self._lock:
            self._require_current_runtime(binding)
            record = self._required_record(binding.session_identity, expired_error=True)
            self._require_exact_binding(record, binding, lease_id)
            return record.lease

    def status(
        self,
        *,
        session_key: str,
        profile: str,
        desktop_controller_present: bool,
    ) -> dict[str, Any]:
        key = profile, session_key
        with self._lock:
            record = self._active_record(key)
            revision = self._revisions.get(key, 0)
            if record is not None:
                return {
                    "controller_kind": "mobile",
                    "controller_label": "Hermes Mobile",
                    "control_revision": revision,
                    "lease_expires_at_epoch_ms": record.lease.expires_at_epoch_ms,
                    "pending_input": None,
                }
            return {
                "controller_kind": "desktop" if desktop_controller_present else "none",
                "controller_label": "Hermes Desktop"
                if desktop_controller_present
                else None,
                "control_revision": revision,
                "lease_expires_at_epoch_ms": 0,
                "pending_input": None,
            }

    def bump_revision(self, *, session_key: str, profile: str) -> int:
        """Advance control state for a pending-input enqueue/resolve/expiry."""
        key = profile, session_key
        with self._lock:
            revision = self._next_revision(key)
            record = self._records.get(key)
            if record is not None and self._monotonic() < record.expires_at_monotonic:
                record.lease = ControlLease(
                    lease_id=record.lease.lease_id,
                    expires_at_epoch_ms=record.lease.expires_at_epoch_ms,
                    control_revision=revision,
                )
            return revision

    def revision(self, *, session_key: str, profile: str) -> int:
        with self._lock:
            self._active_record((profile, session_key))
            return self._revisions.get((profile, session_key), 0)

    def transport_disconnected(self, transport_id: str) -> None:
        if not transport_id:
            return
        with self._lock:
            now = self._monotonic()
            for record in self._records.values():
                if record.binding.transport_id == transport_id:
                    record.disconnected_at_monotonic = now

    def revoke_all(self) -> None:
        """Revoke every lease after an authoritative runtime identity change."""
        with self._lock:
            self._revoke_all_locked()

    def _revoke_all_locked(self) -> None:
        keys = tuple(self._records)
        self._records.clear()
        for key in keys:
            self._next_revision(key)

    def _require_current_runtime(self, binding: ControlBinding) -> None:
        if self._runtime_identity is None:
            return
        if (
            not self._runtime_ready
            or binding.session_identity[0] != (self._runtime_identity[0])
            or binding.runtime_generation != self._runtime_identity[1]
        ):
            raise SessionBindingMismatch("session binding mismatch")

    def _active_record(self, key: tuple[str, str]) -> _LeaseRecord | None:
        record = self._records.get(key)
        if record is None:
            return None
        if self._monotonic() < record.expires_at_monotonic:
            return record
        self._records.pop(key, None)
        self._next_revision(key)
        return None

    def _required_record(
        self,
        key: tuple[str, str],
        *,
        expired_error: bool,
    ) -> _LeaseRecord:
        record = self._records.get(key)
        if record is None:
            raise LeaseRequired("controller lease required")
        if self._monotonic() >= record.expires_at_monotonic:
            self._records.pop(key, None)
            self._next_revision(key)
            if expired_error:
                raise LeaseExpired("controller lease expired")
            raise LeaseRequired("controller lease required")
        return record

    def _may_rebind(self, record: _LeaseRecord, binding: ControlBinding) -> bool:
        disconnected_at = record.disconnected_at_monotonic
        if disconnected_at is None:
            return False
        same_authenticated_client = (
            record.binding.session_identity == binding.session_identity
            and record.binding.runtime_generation == binding.runtime_generation
            and record.binding.runtime_session_id == binding.runtime_session_id
            and record.binding.user_id == binding.user_id
            and record.binding.provider == binding.provider
            and record.binding.client_instance_id == binding.client_instance_id
            and record.binding.transport_id != binding.transport_id
        )
        return same_authenticated_client and (
            self._monotonic() - disconnected_at <= self._reconnect_grace_seconds
        )

    def _mint(self, binding: ControlBinding) -> ControlLease:
        key = binding.session_identity
        revision = self._next_revision(key)
        lease = ControlLease(
            lease_id=self._lease_id_factory(),
            expires_at_epoch_ms=int((self._epoch_time() + self._ttl_seconds) * 1000),
            control_revision=revision,
        )
        if not lease.lease_id:
            raise RuntimeError("lease id factory returned an empty value")
        self._records[key] = _LeaseRecord(
            lease=lease,
            binding=binding,
            expires_at_monotonic=self._monotonic() + self._ttl_seconds,
        )
        return lease

    def _require_exact_binding(
        self,
        record: _LeaseRecord,
        binding: ControlBinding,
        lease_id: str,
    ) -> None:
        if not lease_id or not secrets.compare_digest(record.lease.lease_id, lease_id):
            raise LeaseMismatch("controller lease binding mismatch")
        if record.binding != binding:
            raise LeaseMismatch("controller lease binding mismatch")

    def _next_revision(self, key: tuple[str, str]) -> int:
        revision = self._revisions.get(key, 0) + 1
        self._revisions[key] = revision
        return revision
