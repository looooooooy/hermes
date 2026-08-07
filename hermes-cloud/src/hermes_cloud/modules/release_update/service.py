"""Deterministic release update policy for Hermes Cloud.

The service decides whether a device should be offered a signed release bundle. It never
returns OSS long-lived credentials; download access is delegated to a short-lived grant
issuer. Runtime Manager still independently verifies Release/Channel/Block signatures and
anti-rollback state before any local download or activation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from .domain import (
    DeviceUpdateContextV1,
    ReleaseUpdateCandidateV1,
    UpdateDecisionStatusV1,
    UpdateDecisionV1,
)
from .ports import DownloadGrantIssuerPort, OsCompatibilityPort, ReleaseCatalogPort


class UpdateCheckPolicyError(RuntimeError):
    """Release update policy or grant output is invalid."""


class UpdateCheckService:
    def __init__(
        self,
        *,
        catalog: ReleaseCatalogPort,
        grant_issuer: DownloadGrantIssuerPort,
        os_compatibility: OsCompatibilityPort,
    ) -> None:
        self._catalog = catalog
        self._grant_issuer = grant_issuer
        self._os_compatibility = os_compatibility

    def check(
        self,
        context: DeviceUpdateContextV1,
        *,
        now: datetime | None = None,
    ) -> UpdateDecisionV1:
        _validate_context(context)
        observed_now = _utc(now or datetime.now(UTC))
        candidate = self._catalog.select_candidate(context)
        if candidate is None:
            return _empty_decision(context, UpdateDecisionStatusV1.UP_TO_DATE, "no_candidate")
        _validate_candidate(candidate)

        if candidate.channel != context.requested_channel:
            return _candidate_decision(
                context, candidate, UpdateDecisionStatusV1.INELIGIBLE, "channel_mismatch"
            )
        if candidate.target != context.target:
            return _candidate_decision(
                context, candidate, UpdateDecisionStatusV1.INELIGIBLE, "target_mismatch"
            )
        if context.enterprise_pin_release_id is not None and (
            candidate.release_id != context.enterprise_pin_release_id
        ):
            return _candidate_decision(
                context,
                candidate,
                UpdateDecisionStatusV1.INELIGIBLE,
                "enterprise_pin_mismatch",
            )
        if not self._os_compatibility.is_compatible(
            target=context.target,
            current_os=context.os_version,
            minimum_os=candidate.minimum_os,
        ):
            return _candidate_decision(
                context, candidate, UpdateDecisionStatusV1.INELIGIBLE, "os_incompatible"
            )
        if candidate.blocked:
            return _candidate_decision(
                context, candidate, UpdateDecisionStatusV1.BLOCKED, "candidate_blocked"
            )
        if candidate.release_generation < candidate.minimum_safe_release_generation:
            return _candidate_decision(
                context,
                candidate,
                UpdateDecisionStatusV1.BLOCKED,
                "candidate_below_minimum_safe_generation",
            )
        if (
            context.active_release_id == candidate.release_id
            and context.active_release_generation == candidate.release_generation
        ):
            return _candidate_decision(
                context, candidate, UpdateDecisionStatusV1.UP_TO_DATE, "already_active"
            )

        historical = candidate.release_generation < context.highest_release_generation
        if historical and not candidate.rollback_authorized:
            return _candidate_decision(
                context, candidate, UpdateDecisionStatusV1.INELIGIBLE, "anti_rollback"
            )

        mandatory = _is_mandatory(context, candidate, observed_now)
        bucket = deterministic_rollout_bucket(
            organization_id=context.organization_id,
            device_id=context.device_id,
            release_id=candidate.release_id,
        )
        if not mandatory and bucket >= candidate.rollout_basis_points:
            return _candidate_decision(
                context,
                candidate,
                UpdateDecisionStatusV1.DEFERRED,
                "rollout_cohort_deferred",
                rollout_bucket=bucket,
            )

        grants = tuple(
            self._grant_issuer.issue_grant(
                device_id=context.device_id,
                artifact=artifact,
                now=observed_now,
            )
            for artifact in candidate.artifacts
        )
        _validate_grants(candidate, grants, observed_now)
        status = (
            UpdateDecisionStatusV1.MANDATORY
            if mandatory
            else UpdateDecisionStatusV1.AVAILABLE
        )
        return UpdateDecisionV1(
            status=status,
            reason_code="mandatory_update" if mandatory else "eligible_update",
            release_id=candidate.release_id,
            product_version=candidate.product_version,
            release_generation=candidate.release_generation,
            channel=candidate.channel,
            rollout_bucket=bucket,
            mandatory=mandatory,
            release_envelope=candidate.release_envelope,
            channel_envelope=candidate.channel_envelope,
            block_envelope=candidate.block_envelope,
            download_grants=grants,
        )


def deterministic_rollout_bucket(
    *, organization_id: str, device_id: str, release_id: str
) -> int:
    for label, value in (
        ("organization_id", organization_id),
        ("device_id", device_id),
        ("release_id", release_id),
    ):
        if not _safe_identifier(value, 160):
            raise UpdateCheckPolicyError(f"invalid {label}")
    digest = hashlib.sha256(
        f"hermes-update-cohort-v1\x00{organization_id}\x00{device_id}\x00{release_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def _is_mandatory(
    context: DeviceUpdateContextV1,
    candidate: ReleaseUpdateCandidateV1,
    now: datetime,
) -> bool:
    if context.active_release_generation < candidate.minimum_safe_release_generation:
        return True
    if not candidate.security_critical:
        return False
    if candidate.mandatory_after is None:
        return True
    return now >= _utc(candidate.mandatory_after)


def _validate_context(context: DeviceUpdateContextV1) -> None:
    for label, value, maximum in (
        ("device_id", context.device_id, 160),
        ("organization_id", context.organization_id, 160),
        ("target", context.target, 64),
        ("requested_channel", context.requested_channel, 32),
    ):
        if not _safe_identifier(value, maximum):
            raise UpdateCheckPolicyError(f"invalid {label}")
    if not _safe_text(context.os_version, 128):
        raise UpdateCheckPolicyError("invalid os_version")
    if context.active_release_generation < 0 or context.highest_release_generation < 0:
        raise UpdateCheckPolicyError("release generations must be non-negative")
    if context.highest_release_generation < context.active_release_generation:
        raise UpdateCheckPolicyError("highest release generation cannot trail active generation")
    if context.enterprise_pin_release_id is not None and not _safe_identifier(
        context.enterprise_pin_release_id, 160
    ):
        raise UpdateCheckPolicyError("invalid enterprise_pin_release_id")


def _validate_candidate(candidate: ReleaseUpdateCandidateV1) -> None:
    for label, value, maximum in (
        ("release_id", candidate.release_id, 160),
        ("product_version", candidate.product_version, 64),
        ("channel", candidate.channel, 32),
        ("target", candidate.target, 64),
    ):
        if not _safe_identifier(value, maximum):
            raise UpdateCheckPolicyError(f"invalid candidate {label}")
    if not _safe_text(candidate.minimum_os, 128):
        raise UpdateCheckPolicyError("invalid candidate minimum_os")
    if candidate.release_generation <= 0 or candidate.channel_generation <= 0:
        raise UpdateCheckPolicyError("candidate generations must be positive")
    if not 0 <= candidate.rollout_basis_points <= 10_000:
        raise UpdateCheckPolicyError("rollout_basis_points must be between 0 and 10000")
    if candidate.minimum_safe_release_generation <= 0:
        raise UpdateCheckPolicyError("minimum safe generation must be positive")
    if not candidate.artifacts:
        raise UpdateCheckPolicyError("candidate must contain at least one artifact")
    kinds: set[str] = set()
    for artifact in candidate.artifacts:
        if not _safe_identifier(artifact.kind, 64) or artifact.kind in kinds:
            raise UpdateCheckPolicyError("candidate artifact kinds must be unique and safe")
        kinds.add(artifact.kind)
        if not _safe_object_key(artifact.object_key):
            raise UpdateCheckPolicyError("candidate artifact object_key is invalid")
        if not _lower_sha256(artifact.sha256):
            raise UpdateCheckPolicyError("candidate artifact sha256 is invalid")
        if artifact.size_bytes <= 0 or artifact.size_bytes > 8 * 1024 * 1024 * 1024:
            raise UpdateCheckPolicyError("candidate artifact size is invalid")


def _validate_grants(candidate, grants, now: datetime) -> None:
    if len(grants) != len(candidate.artifacts):
        raise UpdateCheckPolicyError("download-grant count does not match candidate artifacts")
    expected = {artifact.object_key: artifact for artifact in candidate.artifacts}
    observed: set[str] = set()
    for grant in grants:
        artifact = expected.get(grant.object_key)
        if artifact is None or grant.object_key in observed:
            raise UpdateCheckPolicyError("download-grant object key is missing or duplicated")
        observed.add(grant.object_key)
        if grant.sha256 != artifact.sha256 or grant.size_bytes != artifact.size_bytes:
            raise UpdateCheckPolicyError("download-grant digest/size does not match signed artifact")
        if not grant.url.startswith("https://") or len(grant.url) > 4096:
            raise UpdateCheckPolicyError("download grant URL must be bounded HTTPS")
        expires_at = _utc(grant.expires_at)
        if expires_at <= now or expires_at > now + timedelta(minutes=20):
            raise UpdateCheckPolicyError("download-grant expiry must be short-lived")


def _candidate_decision(
    context: DeviceUpdateContextV1,
    candidate: ReleaseUpdateCandidateV1,
    status: UpdateDecisionStatusV1,
    reason_code: str,
    *,
    rollout_bucket: int | None = None,
) -> UpdateDecisionV1:
    return UpdateDecisionV1(
        status=status,
        reason_code=reason_code,
        release_id=candidate.release_id,
        product_version=candidate.product_version,
        release_generation=candidate.release_generation,
        channel=candidate.channel,
        rollout_bucket=rollout_bucket,
        mandatory=False,
        release_envelope=candidate.release_envelope,
        channel_envelope=candidate.channel_envelope,
        block_envelope=candidate.block_envelope,
        download_grants=(),
    )


def _empty_decision(
    context: DeviceUpdateContextV1,
    status: UpdateDecisionStatusV1,
    reason_code: str,
) -> UpdateDecisionV1:
    return UpdateDecisionV1(
        status=status,
        reason_code=reason_code,
        release_id=None,
        product_version=None,
        release_generation=None,
        channel=context.requested_channel,
        rollout_bucket=None,
        mandatory=False,
        release_envelope=None,
        channel_envelope=None,
        block_envelope=None,
        download_grants=(),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise UpdateCheckPolicyError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _safe_identifier(value: str, maximum: int) -> bool:
    return bool(value) and len(value) <= maximum and all(
        ch.isalnum() or ch in ".:_+-" for ch in value
    )


def _safe_text(value: str, maximum: int) -> bool:
    return (
        bool(value)
        and len(value) <= maximum
        and "\x00" not in value
        and all(character.isprintable() for character in value)
    )


def _safe_object_key(value: str) -> bool:
    if not value or len(value) > 1024 or value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} and _safe_identifier(part, 255) for part in parts)


def _lower_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
