from __future__ import annotations

from hermes_release_publisher import (
    BucketEvidence,
    BucketMap,
    LifecycleEvidence,
    OssDoctorPolicyV1,
    OssRepositoryDoctor,
)


class FakeDoctorBackend:
    def __init__(self, evidence):
        self.evidence = evidence

    def inspect_bucket(self, bucket: str, *, include_cnames: bool):
        return self.evidence[bucket]


def buckets() -> BucketMap:
    return BucketMap(
        staging="hermes-release-staging",
        artifacts="hermes-release-artifacts",
        control="hermes-release-control",
        evidence="hermes-release-evidence",
    )


def healthy() -> dict[str, BucketEvidence]:
    return {
        "hermes-release-staging": BucketEvidence(
            True,
            acl="private",
            encryption="AES256",
            lifecycle=LifecycleEvidence(
                rule_ids=("staging-cleanup",),
                has_expiration=True,
                has_abort_multipart=True,
            ),
        ),
        "hermes-release-artifacts": BucketEvidence(
            True,
            acl="private",
            versioning="Enabled",
            encryption="KMS",
            lifecycle=LifecycleEvidence(
                rule_ids=("artifact-versions",),
                has_noncurrent_expiration=True,
            ),
            cnames=("updates.example.com",),
        ),
        "hermes-release-control": BucketEvidence(
            True,
            acl="private",
            versioning="Enabled",
            encryption="AES256",
            lifecycle=LifecycleEvidence(
                rule_ids=("control-versions",),
                has_noncurrent_expiration=True,
            ),
        ),
        "hermes-release-evidence": BucketEvidence(
            True,
            acl="private",
            encryption="KMS",
        ),
    }


def test_healthy_repository_passes_all_doctor_gates() -> None:
    evidence = healthy()
    doctor = OssRepositoryDoctor(
        FakeDoctorBackend(evidence),
        buckets(),
        OssDoctorPolicyV1(required_cname="updates.example.com"),
    )

    report = doctor.run()

    assert report.passed is True
    assert all(check.passed for check in report.checks)
    assert report.buckets["artifacts"].versioning == "Enabled"


def test_public_bucket_fails_repository_doctor() -> None:
    evidence = healthy()
    evidence["hermes-release-control"] = BucketEvidence(
        True,
        acl="public-read",
        versioning="Enabled",
        encryption="AES256",
        lifecycle=LifecycleEvidence(has_noncurrent_expiration=True),
    )
    report = OssRepositoryDoctor(
        FakeDoctorBackend(evidence), buckets(), OssDoctorPolicyV1()
    ).run()

    assert report.passed is False
    assert any(check.name == "control.acl.private" and not check.passed for check in report.checks)


def test_suspended_versioning_and_missing_noncurrent_policy_fail() -> None:
    evidence = healthy()
    evidence["hermes-release-artifacts"] = BucketEvidence(
        True,
        acl="private",
        versioning="Suspended",
        encryption="AES256",
        lifecycle=LifecycleEvidence(rule_ids=("bad",)),
    )
    report = OssRepositoryDoctor(
        FakeDoctorBackend(evidence), buckets(), OssDoctorPolicyV1()
    ).run()

    assert report.passed is False
    names = {check.name for check in report.checks if not check.passed}
    assert "artifacts.versioning.enabled" in names
    assert "artifacts.lifecycle.noncurrent_expiration" in names


def test_missing_staging_abort_multipart_policy_fails() -> None:
    evidence = healthy()
    evidence["hermes-release-staging"] = BucketEvidence(
        True,
        acl="private",
        encryption="AES256",
        lifecycle=LifecycleEvidence(has_expiration=True, has_abort_multipart=False),
    )
    report = OssRepositoryDoctor(
        FakeDoctorBackend(evidence), buckets(), OssDoctorPolicyV1()
    ).run()

    assert report.passed is False
    assert any(check.name == "staging.lifecycle.abort_multipart" and not check.passed for check in report.checks)


def test_required_cname_must_be_enabled_on_artifact_origin() -> None:
    evidence = healthy()
    evidence["hermes-release-artifacts"] = BucketEvidence(
        True,
        acl="private",
        versioning="Enabled",
        encryption="AES256",
        lifecycle=LifecycleEvidence(has_noncurrent_expiration=True),
        cnames=(),
    )
    report = OssRepositoryDoctor(
        FakeDoctorBackend(evidence),
        buckets(),
        OssDoctorPolicyV1(required_cname="updates.example.com"),
    ).run()

    assert report.passed is False
    assert any(check.name == "artifacts.cname" and not check.passed for check in report.checks)
