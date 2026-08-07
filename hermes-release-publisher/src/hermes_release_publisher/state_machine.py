from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .repository import PublisherError


class ReleaseState(str, Enum):
    DRAFT = "DRAFT"
    BUILT = "BUILT"
    QUALIFIED = "QUALIFIED"
    SIGNED = "SIGNED"
    UPLOADED = "UPLOADED"
    VERIFIED = "VERIFIED"
    CANARY = "CANARY"
    BETA = "BETA"
    STABLE = "STABLE"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"
    BLOCKED = "BLOCKED"
    REVOKED = "REVOKED"


_FORWARD = {
    ReleaseState.DRAFT: {ReleaseState.BUILT},
    ReleaseState.BUILT: {ReleaseState.QUALIFIED},
    ReleaseState.QUALIFIED: {ReleaseState.SIGNED},
    ReleaseState.SIGNED: {ReleaseState.UPLOADED},
    ReleaseState.UPLOADED: {ReleaseState.VERIFIED},
    ReleaseState.VERIFIED: {ReleaseState.CANARY},
    ReleaseState.CANARY: {ReleaseState.BETA},
    ReleaseState.BETA: {ReleaseState.STABLE},
    ReleaseState.STABLE: {ReleaseState.DEPRECATED},
    ReleaseState.DEPRECATED: {ReleaseState.ARCHIVED},
    ReleaseState.ARCHIVED: set(),
    ReleaseState.BLOCKED: set(),
    ReleaseState.REVOKED: set(),
}

_BLOCKABLE = {
    ReleaseState.QUALIFIED,
    ReleaseState.SIGNED,
    ReleaseState.UPLOADED,
    ReleaseState.VERIFIED,
    ReleaseState.CANARY,
    ReleaseState.BETA,
    ReleaseState.STABLE,
    ReleaseState.DEPRECATED,
}

_REVOKABLE = {
    ReleaseState.SIGNED,
    ReleaseState.UPLOADED,
    ReleaseState.VERIFIED,
    ReleaseState.CANARY,
    ReleaseState.BETA,
    ReleaseState.STABLE,
    ReleaseState.DEPRECATED,
    ReleaseState.BLOCKED,
}


@dataclass(frozen=True)
class ReleaseStateRecordV1:
    schema_version: int
    release_id: str
    transition_generation: int
    state: ReleaseState
    previous_state: ReleaseState | None
    reason_code: str
    occurred_at: str
    evidence_sha256: str | None = None


class ReleaseStateMachine:
    @staticmethod
    def transition(
        current: ReleaseStateRecordV1,
        *,
        next_state: ReleaseState,
        transition_generation: int,
        reason_code: str,
        occurred_at: str,
        evidence_sha256: str | None = None,
    ) -> ReleaseStateRecordV1:
        _validate_record(current)
        if transition_generation <= current.transition_generation:
            raise PublisherError("release transition generation must increase monotonically")
        if not _identifier(reason_code, 96):
            raise PublisherError("release transition reason_code is invalid")
        if not occurred_at or occurred_at != occurred_at.strip() or not occurred_at.endswith("Z"):
            raise PublisherError("release transition occurred_at must be canonical UTC text")
        if evidence_sha256 is not None and not _sha256(evidence_sha256):
            raise PublisherError("release transition evidence SHA-256 is invalid")
        if next_state == current.state:
            raise PublisherError("release state transition cannot be a no-op")

        allowed = next_state in _FORWARD[current.state]
        if next_state == ReleaseState.BLOCKED:
            allowed = current.state in _BLOCKABLE
        elif next_state == ReleaseState.REVOKED:
            allowed = current.state in _REVOKABLE
        if not allowed:
            raise PublisherError(f"illegal release state transition: {current.state.value} -> {next_state.value}")

        return ReleaseStateRecordV1(
            schema_version=1,
            release_id=current.release_id,
            transition_generation=transition_generation,
            state=next_state,
            previous_state=current.state,
            reason_code=reason_code,
            occurred_at=occurred_at,
            evidence_sha256=evidence_sha256,
        )

    @staticmethod
    def initial(*, release_id: str, occurred_at: str) -> ReleaseStateRecordV1:
        if not release_id or len(release_id) > 128 or release_id != release_id.strip():
            raise PublisherError("release_id is invalid")
        if not occurred_at or not occurred_at.endswith("Z"):
            raise PublisherError("release state occurred_at must be canonical UTC text")
        return ReleaseStateRecordV1(
            schema_version=1,
            release_id=release_id,
            transition_generation=1,
            state=ReleaseState.DRAFT,
            previous_state=None,
            reason_code="created",
            occurred_at=occurred_at,
            evidence_sha256=None,
        )


def _validate_record(record: ReleaseStateRecordV1) -> None:
    if record.schema_version != 1 or record.transition_generation <= 0:
        raise PublisherError("release state record is invalid")
    if not record.release_id or len(record.release_id) > 128 or record.release_id != record.release_id.strip():
        raise PublisherError("release state release_id is invalid")
    if record.state == ReleaseState.DRAFT and record.previous_state is not None:
        raise PublisherError("initial DRAFT state must not have previous_state")
    if record.state != ReleaseState.DRAFT and record.previous_state is None:
        raise PublisherError("non-initial release state must include previous_state")


def _identifier(value: str, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(character.isalnum() or character in "._-" for character in value)
    )


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
