"""Bounded idempotency ledger for explicit-control mutations."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class CommandLedgerError(Exception):
    """Base class for display-safe command ledger failures."""


class RequestPayloadConflict(CommandLedgerError):
    """A request ID was reused with a different canonical payload."""


class CommandOwnershipMismatch(CommandLedgerError):
    """A request belongs to another authenticated client instance."""


@dataclass(frozen=True)
class CommandIdentity:
    session_key: str
    user_id: str
    provider: str
    client_instance_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.session_key,
                self.user_id,
                self.provider,
                self.client_instance_id,
            )
        ):
            raise ValueError("command identity fields must be non-empty strings")

    @property
    def principal_key(self) -> tuple[str, str, str]:
        return self.session_key, self.user_id, self.provider


@dataclass(frozen=True, repr=False)
class CommandLedgerResult:
    method: str
    client_request_id: str
    result: dict[str, Any]
    replayed: bool

    def __repr__(self) -> str:
        status = self.result.get("status")
        return (
            "CommandLedgerResult("
            f"method={self.method!r}, client_request_id={self.client_request_id!r}, "
            f"status={status!r}, replayed={self.replayed})"
        )


@dataclass
class _Entry:
    method: str
    client_request_id: str
    client_instance_id: str
    payload_digest: str
    condition: threading.Condition
    completed: bool = False
    result: dict[str, Any] | None = None
    expires_at: float = float("inf")


class CommandLedger:
    """Execute each canonical mutation once and retain a bounded query result."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 15 * 60,
        max_entries: int = 1024,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._entries: OrderedDict[
            tuple[str, str, str, str, str],
            _Entry,
        ] = OrderedDict()

    def execute(
        self,
        identity: CommandIdentity,
        *,
        method: str,
        client_request_id: str,
        payload: Mapping[str, Any],
        operation: Callable[[], Mapping[str, Any]],
    ) -> CommandLedgerResult:
        method = self._required_text(method, "method")
        request_id = self._required_text(client_request_id, "client_request_id")
        digest = self._payload_digest(payload)
        key = (*identity.principal_key, method, request_id)

        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(key)
            if entry is not None:
                if entry.payload_digest != digest:
                    raise RequestPayloadConflict(
                        "client request ID reused with different payload"
                    )
                if entry.client_instance_id != identity.client_instance_id:
                    raise CommandOwnershipMismatch(
                        "client request belongs to another client instance"
                    )
                while not entry.completed:
                    entry.condition.wait()
                self._entries.move_to_end(key)
                return self._public_result(entry, replayed=True)

            entry = _Entry(
                method=method,
                client_request_id=request_id,
                client_instance_id=identity.client_instance_id,
                payload_digest=digest,
                condition=threading.Condition(self._lock),
            )
            self._entries[key] = entry
            self._evict_completed_locked()

        try:
            operation_result = dict(operation())
        except Exception:  # noqa: BLE001 - an owner action may already have taken effect.
            # The owner action may have crossed its side-effect boundary before
            # failing. Preserve ambiguity and force status/user reconciliation;
            # never make an automatic resend appear safe.
            operation_result = {"status": "unknown"}

        with self._lock:
            entry.result = copy.deepcopy(operation_result)
            entry.completed = True
            entry.expires_at = self._monotonic() + self._ttl_seconds
            entry.condition.notify_all()
            self._entries.move_to_end(key)
            self._evict_completed_locked()
            return self._public_result(entry, replayed=False)

    def replay(
        self,
        identity: CommandIdentity,
        *,
        method: str,
        client_request_id: str,
        payload: Mapping[str, Any],
    ) -> CommandLedgerResult | None:
        """Return an existing canonical result without reserving a new entry."""
        method = self._required_text(method, "method")
        request_id = self._required_text(client_request_id, "client_request_id")
        digest = self._payload_digest(payload)
        key = (*identity.principal_key, method, request_id)
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.payload_digest != digest:
                raise RequestPayloadConflict(
                    "client request ID reused with different payload"
                )
            if entry.client_instance_id != identity.client_instance_id:
                raise CommandOwnershipMismatch(
                    "client request belongs to another client instance"
                )
            while not entry.completed:
                entry.condition.wait()
            self._entries.move_to_end(key)
            return self._public_result(entry, replayed=True)

    def status(
        self,
        identity: CommandIdentity,
        *,
        client_request_id: str,
        method: str,
    ) -> CommandLedgerResult | None:
        method = self._required_text(method, "method")
        request_id = self._required_text(client_request_id, "client_request_id")
        key = (*identity.principal_key, method, request_id)
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(key)
            if (
                entry is None
                or entry.client_instance_id != identity.client_instance_id
                or not entry.completed
                or entry.result is None
                or entry.result.get("status")
                not in {"accepted", "queued", "rejected"}
            ):
                return None
            self._entries.move_to_end(key)
            result = {
                key: copy.deepcopy(entry.result[key])
                for key in ("status", "client_turn_id", "server_turn_id")
                if key in entry.result
            }
            result["client_request_id"] = entry.client_request_id
            return CommandLedgerResult(
                method=entry.method,
                client_request_id=entry.client_request_id,
                result=result,
                replayed=True,
            )

    def _public_result(self, entry: _Entry, *, replayed: bool) -> CommandLedgerResult:
        return CommandLedgerResult(
            method=entry.method,
            client_request_id=entry.client_request_id,
            result=copy.deepcopy(entry.result or {"status": "unknown"}),
            replayed=replayed,
        )

    def _purge_expired_locked(self) -> None:
        now = self._monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.completed and entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _evict_completed_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            completed_key = next(
                (key for key, entry in self._entries.items() if entry.completed),
                None,
            )
            if completed_key is None:
                return
            self._entries.pop(completed_key, None)

    @staticmethod
    def _payload_digest(payload: Mapping[str, Any]) -> str:
        try:
            canonical = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("command payload must be canonical JSON") from exc
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be a non-empty trimmed string")
        if len(value) > 256:
            raise ValueError(f"{name} is too long")
        return value
