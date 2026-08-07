import alibabacloud_oss_v2 as oss


def test_pinned_oss_sdk_v2_exposes_required_object_operations() -> None:
    put = oss.PutObjectRequest(
        bucket="hermes-release-artifacts",
        key="artifacts/v1/sha256/aa/example/payload.bin",
        metadata={"hermes-sha256": "a" * 64},
        forbid_overwrite=True,
        server_side_encryption="AES256",
    )
    head = oss.HeadObjectRequest(
        bucket="hermes-release-artifacts",
        key="artifacts/v1/sha256/aa/example/payload.bin",
    )

    assert put.bucket == "hermes-release-artifacts"
    assert put.forbid_overwrite is True
    assert put.metadata["hermes-sha256"] == "a" * 64
    assert put.server_side_encryption == "AES256"
    assert head.key.endswith("payload.bin")
    assert hasattr(oss.Client, "put_object")
    assert hasattr(oss.Client, "put_object_from_file")
    assert hasattr(oss.Client, "head_object")
    assert hasattr(oss.exceptions, "OperationError")


def test_pinned_oss_sdk_v2_exposes_repository_doctor_operations() -> None:
    assert oss.GetBucketAclRequest(bucket="hermes-release-artifacts").bucket == "hermes-release-artifacts"
    assert oss.GetBucketVersioningRequest(bucket="hermes-release-artifacts").bucket == "hermes-release-artifacts"
    assert oss.GetBucketEncryptionRequest(bucket="hermes-release-artifacts").bucket == "hermes-release-artifacts"
    assert oss.GetBucketLifecycleRequest(bucket="hermes-release-staging").bucket == "hermes-release-staging"
    assert oss.ListCnameRequest(bucket="hermes-release-artifacts").bucket == "hermes-release-artifacts"

    for method in (
        "get_bucket_acl",
        "get_bucket_versioning",
        "get_bucket_encryption",
        "get_bucket_lifecycle",
        "list_cname",
    ):
        assert hasattr(oss.Client, method), method
