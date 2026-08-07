from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hermes_cloud.modules.release_update import (
    DeviceUpdateContextV1,
    DownloadGrantV1,
    ReleaseArtifactRefV1,
    ReleaseUpdateCandidateV1,
    UpdateCheckPolicyError,
    UpdateCheckService,
    UpdateDecisionStatusV1,
)
from hermes_cloud.modules.release_update.service import deterministic_rollout_bucket

NOW = datetime(2026, 8, 7, 15, 0, tzinfo=UTC)


class StaticCatalog:
    def __init__(self, candidate: ReleaseUpdateCandidateV1 | None) -> None:
        self.candidate = candidate

    def select_candidate(self, context: DeviceUpdateContextV1):
        return self.candidate


class CompatibleOs:
    def __init__(self, compatible: bool = True) -> None:
        self.compatible = compatible

    def is_compatible(self, *, target: str, current_os: str, minimum_os: str) -> bool:
        return self.compatible


class ShortLivedGrantIssuer:
    def __init__(self, *, minutes: int = 10, url_prefix: str = "https://updates.example.com/") -> None:
        self.minutes = minutes
        self.url_prefix = url_prefix
        self.calls: list[str] = []

    def issue_grant(self, *, device_id: str, artifact: ReleaseArtifactRefV1, now: datetime):
        self.calls.append(artifact.object_key)
        return DownloadGrantV1(
            object_key=artifact.object_key,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            url=f"{self.url_prefix}{artifact.object_key}?grant=device",
            expires_at=now + timedelta(minutes=self.minutes),
        )


def test_forward_update_returns_signed_control_and_short_lived_grants() -> None:
    candidate = candidate_fixture(rollout_basis_points=10_000)
    issuer = ShortLivedGrantIssuer()
    decision = service(candidate, issuer=issuer).check(context_fixture(), now=NOW)

    assert decision.status is UpdateDecisionStatusV1.AVAILABLE
    assert decision.reason_code == "eligible_update"
    assert decision.release_id == candidate.release_id
    assert decision.mandatory is False
    assert len(decision.download_grants) == 2
    assert decision.release_envelope == candidate.release_envelope
    assert decision.channel_envelope == candidate.channel_envelope
    assert decision.block_envelope == candidate.block_envelope
    assert issuer.calls == [artifact.object_key for artifact in candidate.artifacts]


def test_rollout_cohort_is_deterministic_and_deferred_without_grants() -> None:
    candidate = candidate_fixture(rollout_basis_points=0)
    issuer = ShortLivedGrantIssuer()
    context = context_fixture()
    first = service(candidate, issuer=issuer).check(context, now=NOW)
    second = service(candidate, issuer=issuer).check(context, now=NOW)

    assert first.status is UpdateDecisionStatusV1.DEFERRED
    assert first.reason_code == "rollout_cohort_deferred"
    assert first.rollout_bucket == second.rollout_bucket
    assert first.rollout_bucket == deterministic_rollout_bucket(
        organization_id=context.organization_id,
        device_id=context.device_id,
        release_id=candidate.release_id,
    )
    assert issuer.calls == []


def test_minimum_safe_generation_bypasses_rollout_and_forces_update() -> None:
    candidate = candidate_fixture(
        rollout_basis_points=0,
        minimum_safe_release_generation=105,
        release_generation=105,
    )
    context = replace(
        context_fixture(),
        active_release_generation=103,
        highest_release_generation=103,
    )
    decision = service(candidate).check(context, now=NOW)

    assert decision.status is UpdateDecisionStatusV1.MANDATORY
    assert decision.mandatory is True
    assert len(decision.download_grants) == 2


def test_security_critical_update_becomes_mandatory_only_after_deadline() -> None:
    candidate = candidate_fixture(
        rollout_basis_points=0,
        security_critical=True,
        mandatory_after=NOW + timedelta(hours=1),
    )
    before = service(candidate).check(context_fixture(), now=NOW)
    after = service(candidate).check(context_fixture(), now=NOW + timedelta(hours=2))

    assert before.status is UpdateDecisionStatusV1.DEFERRED
    assert after.status is UpdateDecisionStatusV1.MANDATORY


def test_enterprise_pin_mismatch_is_ineligible() -> None:
    context = replace(
        context_fixture(), enterprise_pin_release_id="1.0.7+20260807.7.g77777777"
    )
    decision = service(candidate_fixture()).check(context, now=NOW)
    assert decision.status is UpdateDecisionStatusV1.INELIGIBLE
    assert decision.reason_code == "enterprise_pin_mismatch"
    assert decision.download_grants == ()


def test_blocked_candidate_is_never_granted() -> None:
    decision = service(candidate_fixture(blocked=True)).check(context_fixture(), now=NOW)
    assert decision.status is UpdateDecisionStatusV1.BLOCKED
    assert decision.reason_code == "candidate_blocked"
    assert decision.download_grants == ()


def test_historical_release_requires_signed_rollback_authorization() -> None:
    context = replace(
        context_fixture(),
        active_release_id="1.0.8+20260807.8.g88888888",
        active_release_generation=108,
        highest_release_generation=108,
    )
    candidate = candidate_fixture(release_generation=106)
    denied = service(candidate).check(context, now=NOW)
    allowed = service(replace(candidate, rollback_authorized=True)).check(context, now=NOW)

    assert denied.status is UpdateDecisionStatusV1.INELIGIBLE
    assert denied.reason_code == "anti_rollback"
    assert allowed.status is UpdateDecisionStatusV1.AVAILABLE


def test_os_incompatibility_fails_closed_before_grant_issue() -> None:
    issuer = ShortLivedGrantIssuer()
    decision = service(candidate_fixture(), issuer=issuer, compatible=False).check(
        replace(context_fixture(), os_version="Ubuntu 22.04 LTS"), now=NOW
    )
    assert decision.status is UpdateDecisionStatusV1.INELIGIBLE
    assert decision.reason_code == "os_incompatible"
    assert issuer.calls == []


def test_grant_must_be_https_and_expire_within_twenty_minutes() -> None:
    with pytest.raises(UpdateCheckPolicyError, match="bounded HTTPS"):
        service(
            candidate_fixture(),
            issuer=ShortLivedGrantIssuer(url_prefix="http://updates.example.com/"),
        ).check(context_fixture(), now=NOW)

    with pytest.raises(UpdateCheckPolicyError, match="short-lived"):
        service(
            candidate_fixture(),
            issuer=ShortLivedGrantIssuer(minutes=21),
        ).check(context_fixture(), now=NOW)


def test_no_candidate_is_up_to_date_without_grants() -> None:
    decision = UpdateCheckService(
        catalog=StaticCatalog(None),
        grant_issuer=ShortLivedGrantIssuer(),
        os_compatibility=CompatibleOs(),
    ).check(context_fixture(), now=NOW)
    assert decision.status is UpdateDecisionStatusV1.UP_TO_DATE
    assert decision.release_id is None
    assert decision.download_grants == ()


def service(
    candidate: ReleaseUpdateCandidateV1,
    *,
    issuer: ShortLivedGrantIssuer | None = None,
    compatible: bool = True,
) -> UpdateCheckService:
    return UpdateCheckService(
        catalog=StaticCatalog(candidate),
        grant_issuer=issuer or ShortLivedGrantIssuer(),
        os_compatibility=CompatibleOs(compatible),
    )


def context_fixture() -> DeviceUpdateContextV1:
    return DeviceUpdateContextV1(
        device_id="dev_01",
        organization_id="org_01",
        target="linux-x86_64",
        os_version="Ubuntu 24.04.4 LTS",
        active_release_id="1.0.4+20260807.4.g44444444",
        active_release_generation=104,
        highest_release_generation=104,
        requested_channel="stable",
    )


def candidate_fixture(
    *,
    release_generation: int = 105,
    rollout_basis_points: int = 10_000,
    minimum_safe_release_generation: int = 100,
    security_critical: bool = False,
    mandatory_after: datetime | None = None,
    rollback_authorized: bool = False,
    blocked: bool = False,
) -> ReleaseUpdateCandidateV1:
    release_id = f"1.0.{release_generation - 100}+20260807.{release_generation}.g55555555"
    artifacts = (
        ReleaseArtifactRefV1(
            kind="bootstrap_payload",
            object_key="artifacts/v1/sha256/aa/" + "a" * 64 + "/bootstrap.tar.zst",
            sha256="a" * 64,
            size_bytes=1024,
        ),
        ReleaseArtifactRefV1(
            kind="managed_release_payload",
            object_key="artifacts/v1/sha256/bb/" + "b" * 64 + "/runtime.tar.zst",
            sha256="b" * 64,
            size_bytes=2048,
        ),
    )
    return ReleaseUpdateCandidateV1(
        release_id=release_id,
        product_version="1.0.5",
        release_generation=release_generation,
        channel="stable",
        channel_generation=42,
        target="linux-x86_64",
        minimum_os="Ubuntu 24.04 LTS",
        rollout_basis_points=rollout_basis_points,
        minimum_safe_release_generation=minimum_safe_release_generation,
        security_critical=security_critical,
        mandatory_after=mandatory_after,
        rollback_authorized=rollback_authorized,
        blocked=blocked,
        artifacts=artifacts,
        release_envelope={"schema_version": 1, "signature": "release"},
        channel_envelope={"schema_version": 1, "signature": "channel"},
        block_envelope={"schema_version": 1, "signature": "block"},
    )
