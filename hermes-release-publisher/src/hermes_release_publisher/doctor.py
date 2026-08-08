from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .repository import BucketMap, PublisherError


@dataclass(frozen=True)
class LifecycleEvidence:
    rule_ids: tuple[str, ...] = ()
    has_expiration: bool = False
    has_abort_multipart: bool = False
    has_noncurrent_expiration: bool = False


@dataclass(frozen=True)
class BucketEvidence:
    exists: bool
    acl: str | None = None
    versioning: str | None = None
    encryption: str | None = None
    lifecycle: LifecycleEvidence = field(default_factory=LifecycleEvidence)
    cnames: tuple[str, ...] = ()
    request_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OssDoctorPolicyV1:
    schema_version: int = 1
    expected_encryption: tuple[str, ...] = ("AES256", "KMS")
    required_cname: str | None = None
    require_versioning_roles: tuple[str, ...] = ("artifacts", "control")
    require_staging_lifecycle: bool = True
    require_prod_noncurrent_lifecycle: bool = True


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class OssDoctorReportV1:
    schema_version: int
    passed: bool
    buckets: Mapping[str, BucketEvidence]
    checks: tuple[DoctorCheck, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "buckets": {
                role: {
                    "exists": evidence.exists,
                    "acl": evidence.acl,
                    "versioning": evidence.versioning,
                    "encryption": evidence.encryption,
                    "lifecycle": {
                        "rule_ids": list(evidence.lifecycle.rule_ids),
                        "has_expiration": evidence.lifecycle.has_expiration,
                        "has_abort_multipart": evidence.lifecycle.has_abort_multipart,
                        "has_noncurrent_expiration": evidence.lifecycle.has_noncurrent_expiration,
                    },
                    "cnames": list(evidence.cnames),
                    "request_ids": dict(evidence.request_ids),
                }
                for role, evidence in self.buckets.items()
            },
            "checks": [
                {"name": check.name, "passed": check.passed, "detail": check.detail}
                for check in self.checks
            ],
        }


class OssDoctorBackend(Protocol):
    def inspect_bucket(self, bucket: str, *, include_cnames: bool) -> BucketEvidence: ...


class OssRepositoryDoctor:
    def __init__(
        self,
        backend: OssDoctorBackend,
        buckets: BucketMap,
        policy: OssDoctorPolicyV1,
    ) -> None:
        self._backend = backend
        self._buckets = buckets
        self._policy = policy
        if policy.schema_version != 1:
            raise PublisherError("unsupported OSS doctor policy schema")
        if not policy.expected_encryption:
            raise PublisherError("OSS doctor must require at least one encryption method")
        if policy.required_cname is not None and (
            not policy.required_cname
            or policy.required_cname != policy.required_cname.strip().lower()
            or "/" in policy.required_cname
            or ":" in policy.required_cname
        ):
            raise PublisherError("required OSS CNAME is invalid")

    def run(self) -> OssDoctorReportV1:
        role_to_bucket = {
            "staging": self._buckets.staging,
            "artifacts": self._buckets.artifacts,
            "control": self._buckets.control,
            "evidence": self._buckets.evidence,
        }
        evidence: dict[str, BucketEvidence] = {}
        checks: list[DoctorCheck] = []
        for role, bucket in role_to_bucket.items():
            item = self._backend.inspect_bucket(
                bucket,
                include_cnames=(role == "artifacts" and self._policy.required_cname is not None),
            )
            evidence[role] = item
            checks.append(
                DoctorCheck(
                    f"{role}.exists",
                    item.exists,
                    f"bucket={bucket}",
                )
            )
            checks.append(
                DoctorCheck(
                    f"{role}.acl.private",
                    item.acl == "private",
                    f"observed_acl={item.acl!r}",
                )
            )
            checks.append(
                DoctorCheck(
                    f"{role}.encryption",
                    item.encryption in self._policy.expected_encryption,
                    f"observed_encryption={item.encryption!r}, expected={self._policy.expected_encryption}",
                )
            )

        for role in self._policy.require_versioning_roles:
            item = evidence.get(role)
            if item is None:
                raise PublisherError(f"unknown versioning role in doctor policy: {role}")
            checks.append(
                DoctorCheck(
                    f"{role}.versioning.enabled",
                    item.versioning == "Enabled",
                    f"observed_versioning={item.versioning!r}",
                )
            )

        staging = evidence["staging"]
        if self._policy.require_staging_lifecycle:
            checks.append(
                DoctorCheck(
                    "staging.lifecycle.expiration",
                    staging.lifecycle.has_expiration,
                    f"rule_ids={staging.lifecycle.rule_ids}",
                )
            )
            checks.append(
                DoctorCheck(
                    "staging.lifecycle.abort_multipart",
                    staging.lifecycle.has_abort_multipart,
                    f"rule_ids={staging.lifecycle.rule_ids}",
                )
            )

        if self._policy.require_prod_noncurrent_lifecycle:
            for role in ("artifacts", "control"):
                item = evidence[role]
                checks.append(
                    DoctorCheck(
                        f"{role}.lifecycle.noncurrent_expiration",
                        item.lifecycle.has_noncurrent_expiration,
                        f"rule_ids={item.lifecycle.rule_ids}",
                    )
                )

        if self._policy.required_cname is not None:
            artifacts = evidence["artifacts"]
            checks.append(
                DoctorCheck(
                    "artifacts.cname",
                    self._policy.required_cname in artifacts.cnames,
                    f"required={self._policy.required_cname}, observed={artifacts.cnames}",
                )
            )

        return OssDoctorReportV1(
            schema_version=1,
            passed=all(check.passed for check in checks),
            buckets=evidence,
            checks=tuple(checks),
        )


class OssV2DoctorBackend:
    def __init__(self, client: object, oss_module: object) -> None:
        self._client = client
        self._oss = oss_module

    @classmethod
    def from_environment(cls, *, region: str, endpoint: str | None = None) -> "OssV2DoctorBackend":
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as error:
            raise PublisherError("alibabacloud-oss-v2 is not installed") from error
        provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        config = oss.config.load_default()
        config.credentials_provider = provider
        config.region = region
        if endpoint:
            config.endpoint = endpoint
        return cls(oss.Client(config), oss)

    def inspect_bucket(self, bucket: str, *, include_cnames: bool) -> BucketEvidence:
        request_ids: dict[str, str] = {}
        try:
            acl_result = self._client.get_bucket_acl(self._oss.GetBucketAclRequest(bucket=bucket))
        except self._oss.exceptions.OperationError as error:
            if _operation_status(error) == 404:
                return BucketEvidence(exists=False)
            raise PublisherError(f"OSS GetBucketAcl failed for {bucket}: {error}") from error
        request_ids["acl"] = _optional_text(getattr(acl_result, "request_id", None)) or ""

        version_result = self._client.get_bucket_versioning(
            self._oss.GetBucketVersioningRequest(bucket=bucket)
        )
        request_ids["versioning"] = _optional_text(getattr(version_result, "request_id", None)) or ""

        encryption = None
        try:
            encryption_result = self._client.get_bucket_encryption(
                self._oss.GetBucketEncryptionRequest(bucket=bucket)
            )
            request_ids["encryption"] = _optional_text(getattr(encryption_result, "request_id", None)) or ""
            rule = getattr(encryption_result, "server_side_encryption_rule", None)
            default = getattr(rule, "apply_server_side_encryption_by_default", None)
            encryption = _optional_text(getattr(default, "sse_algorithm", None))
        except self._oss.exceptions.OperationError as error:
            if _operation_status(error) != 404:
                raise PublisherError(f"OSS GetBucketEncryption failed for {bucket}: {error}") from error

        lifecycle = LifecycleEvidence()
        try:
            lifecycle_result = self._client.get_bucket_lifecycle(
                self._oss.GetBucketLifecycleRequest(bucket=bucket)
            )
            request_ids["lifecycle"] = _optional_text(getattr(lifecycle_result, "request_id", None)) or ""
            configuration = getattr(lifecycle_result, "lifecycle_configuration", None)
            rules = tuple(getattr(configuration, "rules", None) or ())
            lifecycle = LifecycleEvidence(
                rule_ids=tuple(
                    str(getattr(rule, "id", ""))
                    for rule in rules
                    if getattr(rule, "id", None)
                ),
                has_expiration=any(getattr(rule, "expiration", None) is not None for rule in rules),
                has_abort_multipart=any(
                    getattr(rule, "abort_multipart_upload", None) is not None for rule in rules
                ),
                has_noncurrent_expiration=any(
                    getattr(rule, "noncurrent_version_expiration", None) is not None for rule in rules
                ),
            )
        except self._oss.exceptions.OperationError as error:
            if _operation_status(error) != 404:
                raise PublisherError(f"OSS GetBucketLifecycle failed for {bucket}: {error}") from error

        cnames: tuple[str, ...] = ()
        if include_cnames:
            cname_result = self._client.list_cname(self._oss.ListCnameRequest(bucket=bucket))
            request_ids["cname"] = _optional_text(getattr(cname_result, "request_id", None)) or ""
            cnames = tuple(
                str(getattr(item, "domain", "")).lower()
                for item in tuple(getattr(cname_result, "cnames", None) or ())
                if _optional_text(getattr(item, "domain", None))
                and str(getattr(item, "status", "")) == "Enabled"
            )

        return BucketEvidence(
            exists=True,
            acl=_optional_text(getattr(acl_result, "acl", None)),
            versioning=_optional_text(getattr(version_result, "version_status", None)),
            encryption=encryption,
            lifecycle=lifecycle,
            cnames=cnames,
            request_ids=request_ids,
        )


def _operation_status(error: BaseException) -> int | None:
    kwargs = getattr(error, "kwargs", None)
    if not isinstance(kwargs, dict):
        return None
    service_error = kwargs.get("error")
    status = getattr(service_error, "status_code", None)
    return int(status) if isinstance(status, int) else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
