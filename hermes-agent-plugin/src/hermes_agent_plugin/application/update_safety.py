"""Fail-closed aggregation of authoritative Host facts for safe updates.

The Runtime Manager must never infer Agent activity from process state, database
rows, or Connector traffic.  This adapter reads only the public Host SPI
Session Catalog and per-session ControlSnapshot facts, then emits a bounded,
display-safe count snapshot.  Session identities and pending request IDs never
leave the Agent process.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

MAX_OBSERVED_COUNT = 1_000_000
MAX_PAGE_SIZE = 128
MAX_CATALOG_PAGES = 8_192
_PENDING_ACTIONS = frozenset({"approval.respond", "clarify.respond"})


class UpdateSafetyViolation(RuntimeError):
    """Body-free fail-closed update-safety evidence failure."""


class _RuntimeBinding(Protocol):
    def snapshot(self) -> tuple[str, str, bool]: ...


@dataclass(frozen=True)
class AuthoritativeUpdateSafetySnapshotV1:
    schema_version: int
    profile: str
    runtime_generation: str
    active_tasks: int
    pending_approvals: int
    pending_clarifications: int
    evidence_complete: bool

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "runtime_generation": self.runtime_generation,
            "active_tasks": self.active_tasks,
            "pending_approvals": self.pending_approvals,
            "pending_clarifications": self.pending_clarifications,
            "evidence_complete": self.evidence_complete,
        }


class HostUpdateSafetyAggregator:
    """Create one generation-fenced snapshot from authoritative Host SPI facts."""

    def __init__(
        self,
        *,
        host: object,
        binding: _RuntimeBinding,
        session_catalog_request: Callable[..., object],
        control_scope: Callable[..., object],
    ) -> None:
        if not callable(session_catalog_request) or not callable(control_scope):
            raise TypeError("Host update-safety DTO factories are unavailable")
        if not callable(getattr(host, "session_catalog", None)) or not callable(
            getattr(host, "control_snapshot", None)
        ):
            raise TypeError("Host update-safety SPI is unavailable")
        self._host = host
        self._binding = binding
        self._session_catalog_request = session_catalog_request
        self._control_scope = control_scope

    def snapshot(self) -> AuthoritativeUpdateSafetySnapshotV1:
        profile, runtime_generation = self._current_binding()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_sessions: set[str] = set()
        catalog_revision: int | None = None
        active_tasks = 0
        pending_approvals = 0
        pending_clarifications = 0

        for _page_index in range(MAX_CATALOG_PAGES):
            page = self._host.session_catalog(
                self._session_catalog_request(
                    profile=profile,
                    runtime_generation=runtime_generation,
                    page_size=MAX_PAGE_SIZE,
                    cursor=cursor,
                )
            )
            if (
                _required_text(_field(page, "profile"), "catalog profile", 128)
                != profile
                or _required_text(
                    _field(page, "runtime_generation"),
                    "catalog runtime_generation",
                    256,
                )
                != runtime_generation
            ):
                raise UpdateSafetyViolation("Session Catalog binding changed")

            revision = _nonnegative_int(
                _field(page, "catalog_revision"),
                "catalog_revision",
            )
            if catalog_revision is None:
                catalog_revision = revision
            elif revision != catalog_revision:
                raise UpdateSafetyViolation("Session Catalog revision changed")

            sessions = _sequence(_field(page, "sessions"), "catalog sessions")
            if len(sessions) > MAX_PAGE_SIZE:
                raise UpdateSafetyViolation("Session Catalog page is oversized")
            for entry in sessions:
                session_key, actions = self._validate_entry(
                    entry,
                    profile=profile,
                    runtime_generation=runtime_generation,
                )
                if session_key in seen_sessions:
                    raise UpdateSafetyViolation("Session Catalog contains duplicates")
                seen_sessions.add(session_key)
                if len(seen_sessions) > MAX_OBSERVED_COUNT:
                    raise UpdateSafetyViolation("Session Catalog is oversized")

                running, approvals, clarifications = self._session_evidence(
                    session_key=session_key,
                    actions=actions,
                    profile=profile,
                    runtime_generation=runtime_generation,
                )
                active_tasks = _bounded_add(active_tasks, int(running))
                pending_approvals = _bounded_add(pending_approvals, approvals)
                pending_clarifications = _bounded_add(
                    pending_clarifications,
                    clarifications,
                )

            raw_cursor = _field(page, "next_cursor")
            if raw_cursor is None:
                break
            next_cursor = _required_text(raw_cursor, "catalog cursor", 512)
            if next_cursor in seen_cursors:
                raise UpdateSafetyViolation("Session Catalog cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise UpdateSafetyViolation("Session Catalog page limit exceeded")

        final_profile, final_generation = self._current_binding()
        if final_profile != profile or final_generation != runtime_generation:
            raise UpdateSafetyViolation("Host runtime generation changed during capture")
        return AuthoritativeUpdateSafetySnapshotV1(
            schema_version=1,
            profile=profile,
            runtime_generation=runtime_generation,
            active_tasks=active_tasks,
            pending_approvals=pending_approvals,
            pending_clarifications=pending_clarifications,
            evidence_complete=True,
        )

    def _current_binding(self) -> tuple[str, str]:
        try:
            profile, runtime_generation, ready = self._binding.snapshot()
        except Exception as error:
            raise UpdateSafetyViolation("Host runtime binding is unavailable") from error
        if ready is not True:
            raise UpdateSafetyViolation("Host runtime is not ready")
        return (
            _required_text(profile, "profile", 128, profile=True),
            _required_text(runtime_generation, "runtime_generation", 256),
        )

    @staticmethod
    def _validate_entry(
        entry: object,
        *,
        profile: str,
        runtime_generation: str,
    ) -> tuple[str, frozenset[str]]:
        if (
            _required_text(_field(entry, "profile"), "session profile", 128)
            != profile
            or _required_text(
                _field(entry, "runtime_generation"),
                "session runtime_generation",
                256,
            )
            != runtime_generation
        ):
            raise UpdateSafetyViolation("Session Catalog entry binding changed")
        session_key = _required_text(
            _field(entry, "durable_session_key"),
            "durable_session_key",
            256,
        )
        raw_actions = _field(entry, "available_actions")
        if isinstance(raw_actions, (str, bytes, bytearray, Mapping)) or not isinstance(
            raw_actions,
            Collection,
        ):
            raise UpdateSafetyViolation("Session Catalog actions are invalid")
        actions = frozenset(raw_actions)
        if len(actions) > 64 or any(
            not isinstance(action, str)
            or not action
            or action != action.strip()
            or len(action) > 128
            for action in actions
        ):
            raise UpdateSafetyViolation("Session Catalog actions are invalid")
        return session_key, actions

    def _session_evidence(
        self,
        *,
        session_key: str,
        actions: frozenset[str],
        profile: str,
        runtime_generation: str,
    ) -> tuple[bool, int, int]:
        snapshot = self._host.control_snapshot(
            self._control_scope(
                profile=profile,
                durable_session_key=session_key,
                runtime_generation=runtime_generation,
            )
        )
        if (
            _required_text(_field(snapshot, "profile"), "snapshot profile", 128)
            != profile
            or _required_text(
                _field(snapshot, "durable_session_key"),
                "snapshot durable_session_key",
                256,
            )
            != session_key
            or _required_text(
                _field(snapshot, "runtime_generation"),
                "snapshot runtime_generation",
                256,
            )
            != runtime_generation
        ):
            raise UpdateSafetyViolation("ControlSnapshot binding changed")
        _nonnegative_int(_field(snapshot, "control_revision"), "control_revision")
        payload = _field(snapshot, "payload")
        if not isinstance(payload, Mapping):
            raise UpdateSafetyViolation("ControlSnapshot payload is invalid")
        status = payload.get("status")
        if status not in {"running", "waiting"}:
            raise UpdateSafetyViolation("ControlSnapshot status is unavailable")
        approvals, clarifications = _pending_counts(payload, actions)
        return status == "running", approvals, clarifications


def _pending_counts(
    payload: Mapping[str, object],
    actions: frozenset[str],
) -> tuple[int, int]:
    exact = payload.get("pending_interaction_counts")
    raw_ids = payload.get("pending_request_ids")
    pending_ids = None if raw_ids is None else _pending_id_sequence(raw_ids)

    if exact is not None:
        if not isinstance(exact, Mapping) or set(exact) != {"approval", "clarify"}:
            raise UpdateSafetyViolation("pending interaction counts are invalid")
        approvals = _bounded_count(exact.get("approval"), "pending approvals")
        clarifications = _bounded_count(
            exact.get("clarify"),
            "pending clarifications",
        )
        if pending_ids is not None and len(pending_ids) != approvals + clarifications:
            raise UpdateSafetyViolation("pending interaction evidence disagrees")
        return approvals, clarifications

    if pending_ids is None:
        if actions & _PENDING_ACTIONS:
            raise UpdateSafetyViolation("pending interaction evidence is unavailable")
        return 0, 0
    if pending_ids:
        # Older Core builds expose opaque request IDs but not their kind.  Never
        # guess whether they are approvals or clarifications; defer the update.
        raise UpdateSafetyViolation("pending interaction kinds are unavailable")
    return 0, 0


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        if name not in value:
            raise UpdateSafetyViolation("Host update-safety fact is incomplete")
        return value[name]
    try:
        return getattr(value, name)
    except AttributeError as error:
        raise UpdateSafetyViolation("Host update-safety fact is incomplete") from error


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value,
        Sequence,
    ):
        raise UpdateSafetyViolation(f"{name} are invalid")
    return value


def _pending_id_sequence(value: object) -> tuple[str, ...]:
    sequence = _sequence(value, "pending request IDs")
    if len(sequence) > MAX_OBSERVED_COUNT:
        raise UpdateSafetyViolation("pending request IDs are oversized")
    result = tuple(
        _required_text(item, "pending request ID", 256) for item in sequence
    )
    if len(result) != len(set(result)):
        raise UpdateSafetyViolation("pending request IDs contain duplicates")
    return result


def _required_text(
    value: object,
    name: str,
    maximum: int,
    *,
    profile: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or (
            profile
            and any(
                not (character.isascii() and (character.isalnum() or character in "_.-"))
                for character in value
            )
        )
    ):
        raise UpdateSafetyViolation(f"{name} is invalid")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise UpdateSafetyViolation(f"{name} is invalid")
    return value


def _bounded_count(value: object, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result > MAX_OBSERVED_COUNT:
        raise UpdateSafetyViolation(f"{name} is out of bounds")
    return result


def _bounded_add(left: int, right: int) -> int:
    result = left + right
    if result > MAX_OBSERVED_COUNT:
        raise UpdateSafetyViolation("Host update-safety count is out of bounds")
    return result


__all__ = [
    "MAX_OBSERVED_COUNT",
    "AuthoritativeUpdateSafetySnapshotV1",
    "HostUpdateSafetyAggregator",
    "UpdateSafetyViolation",
]
