from __future__ import annotations

import os
import stat
from typing import Any

import pytest

import hermes_cloud.configuration as configuration_module
from hermes_cloud.configuration import (
    CloudConfig,
    ConfigurationError,
    DsnFileReference,
)


def test_configuration_rejects_direct_dsn_without_leaking_it() -> None:
    secret = "unit-test-secret-value"

    with pytest.raises(ConfigurationError) as caught:
        CloudConfig.from_mapping({"HERMES_POSTGRES_DSN": secret})

    assert secret not in str(caught.value)
    assert "unit-test-secret" not in str(caught.value)


@pytest.mark.parametrize("key", ["DATABASE_URL", "POSTGRES_URL", "REDIS_DSN"])
def test_configuration_rejects_non_reference_secret_keys(key: str) -> None:
    with pytest.raises(ConfigurationError):
        CloudConfig.from_mapping({key: "sensitive-value"})


def test_dsn_file_is_read_only_when_regular_non_symlink_and_owner_only(
    tmp_path: Any,
) -> None:
    secret = "unit-test-secret-value"
    secret_file = tmp_path / "postgres.dsn"
    secret_file.write_text(secret, encoding="utf-8")
    secret_file.chmod(0o600)
    config = CloudConfig.from_mapping(
        {
            "HERMES_POSTGRES_DSN_FILE": os.fspath(secret_file),
            "HERMES_KMS_SECRET_REF": "secret-manager/project/hermes/postgres",
        }
    )

    assert config.read_dsn("HERMES_POSTGRES_DSN_FILE") == secret
    assert config.safe_summary() == {
        "dsn_files": ["HERMES_POSTGRES_DSN_FILE"],
        "secret_refs": ["HERMES_KMS_SECRET_REF"],
    }


def test_dsn_file_rejects_symlink(tmp_path: Any) -> None:
    target = tmp_path / "postgres.dsn"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o600)
    symlink = tmp_path / "postgres-link.dsn"
    symlink.symlink_to(target)
    config = CloudConfig.from_mapping({"HERMES_POSTGRES_DSN_FILE": os.fspath(symlink)})

    with pytest.raises(ConfigurationError):
        config.read_dsn("HERMES_POSTGRES_DSN_FILE")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_dsn_file_rejects_group_or_other_permissions(
    tmp_path: Any,
    mode: int,
) -> None:
    secret_file = tmp_path / f"postgres-{mode:o}.dsn"
    secret_file.write_text("secret", encoding="utf-8")
    secret_file.chmod(mode)
    config = CloudConfig.from_mapping(
        {"HERMES_POSTGRES_DSN_FILE": os.fspath(secret_file)}
    )

    with pytest.raises(ConfigurationError):
        config.read_dsn("HERMES_POSTGRES_DSN_FILE")


def test_dsn_file_rejects_directory(tmp_path: Any) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir(mode=0o700)
    config = CloudConfig.from_mapping(
        {"HERMES_POSTGRES_DSN_FILE": os.fspath(directory)}
    )

    with pytest.raises(ConfigurationError):
        config.read_dsn("HERMES_POSTGRES_DSN_FILE")


def test_unknown_or_empty_secret_reference_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        CloudConfig.from_mapping({"UNRELATED_SETTING": "value"})

    with pytest.raises(ConfigurationError):
        CloudConfig.from_mapping({"HERMES_KMS_SECRET_REF": ""})


def test_dsn_file_rejects_owner_other_than_current_uid(tmp_path: Any) -> None:
    secret_file = tmp_path / "postgres.dsn"
    secret_file.write_text("unit-test-secret-value", encoding="utf-8")
    secret_file.chmod(0o600)
    owner_uid = secret_file.stat().st_uid
    reference = DsnFileReference(
        os.fspath(secret_file),
        owner_uid_provider=lambda: owner_uid + 1,
    )

    with pytest.raises(ConfigurationError) as caught:
        reference.read()

    assert os.fspath(secret_file) not in str(caught.value)
    assert "unit-test-secret" not in str(caught.value)


def test_dsn_file_rejects_content_larger_than_64_kib_without_leaking_path(
    tmp_path: Any,
) -> None:
    secret_file = tmp_path / "oversize-postgres.dsn"
    secret_file.write_bytes(b"x" * (64 * 1024 + 1))
    secret_file.chmod(0o600)
    config = CloudConfig.from_mapping(
        {"HERMES_POSTGRES_DSN_FILE": os.fspath(secret_file)}
    )

    with pytest.raises(ConfigurationError) as caught:
        config.read_dsn("HERMES_POSTGRES_DSN_FILE")

    assert os.fspath(secret_file) not in str(caught.value)


def test_dsn_file_checks_path_identity_before_and_after_read(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = tmp_path / "postgres.dsn"
    secret_file.write_text("unit-test-secret-value", encoding="utf-8")
    secret_file.chmod(0o600)
    original_lstat = os.lstat
    calls = 0

    def changing_lstat(path: str) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = original_lstat(path)
        if calls < 2:
            return result
        values = list(result)
        values[stat.ST_INO] += 1
        return os.stat_result(values)

    monkeypatch.setattr(configuration_module.os, "lstat", changing_lstat)
    config = CloudConfig.from_mapping(
        {"HERMES_POSTGRES_DSN_FILE": os.fspath(secret_file)}
    )

    with pytest.raises(ConfigurationError):
        config.read_dsn("HERMES_POSTGRES_DSN_FILE")

    assert calls >= 2
