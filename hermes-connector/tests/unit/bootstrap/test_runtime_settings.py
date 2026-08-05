from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import hermes_connector.bootstrap.settings as settings_module
from hermes_connector.adapters.platform.macos.credentials import (
    CloudCredentialUnavailable,
    MacOSFileCloudTokenProvider,
    MacOSKeychainCloudTokenProvider,
    UnsafeCredentialFile,
)
from hermes_connector.bootstrap.settings import (
    RuntimeConfigurationError,
    load_runtime_settings,
)


def _valid_environment(tmp_path: Path) -> dict[str, str]:
    state_directory = tmp_path / "state"
    role_directories = {
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR": tmp_path / "local-registry",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR": tmp_path / "local-sockets",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR": tmp_path / "control-registry",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR": tmp_path / "control-sockets",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR": tmp_path / "observer-registry",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR": tmp_path / "observer-sockets",
    }
    for directory in (state_directory, *role_directories.values()):
        directory.mkdir(mode=0o700)
    token_file = tmp_path / "cloud-token"
    token_file.write_text("opaque-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    return {
        "HERMES_CONNECTOR_CLOUD_ENDPOINT": (
            "wss://cloud.example.test/hermes/internal/connector/ws"
        ),
        "HERMES_CONNECTOR_API_ENDPOINT": "https://cloud.example.test/hermes",
        "HERMES_CONNECTOR_PROFILE": "default",
        "HERMES_CONNECTOR_VERSION": "1.2.3",
        **{key: str(path) for key, path in role_directories.items()},
        "HERMES_CONNECTOR_STATE_DIR": str(state_directory),
        "HERMES_CONNECTOR_DATABASE_FILE": str(state_directory / "connector.sqlite3"),
        "HERMES_CONNECTOR_LOCK_FILE": str(state_directory / "connector.lock"),
        "HERMES_CONNECTOR_CREDENTIAL_STORE": "file",
        "HERMES_CONNECTOR_TOKEN_FILE": str(token_file),
    }


def _use_six_role_paths(
    environment: dict[str, str],
    tmp_path: Path,
) -> dict[str, Path]:
    environment.pop("HERMES_CONNECTOR_REGISTRY_DIR", None)
    environment.pop("HERMES_CONNECTOR_SOCKET_DIR", None)
    paths = {
        "local_gateway_registry_directory": tmp_path / "local-registry",
        "local_gateway_socket_directory": tmp_path / "local-sockets",
        "control_registry_directory": tmp_path / "control-registry",
        "control_socket_directory": tmp_path / "control-sockets",
        "observer_registry_directory": tmp_path / "observer-registry",
        "observer_socket_directory": tmp_path / "observer-sockets",
    }
    environment_keys = {
        "local_gateway_registry_directory": (
            "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR"
        ),
        "local_gateway_socket_directory": ("HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR"),
        "control_registry_directory": "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "control_socket_directory": "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "observer_registry_directory": "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "observer_socket_directory": "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    }
    for field, path in paths.items():
        path.mkdir(mode=0o700, exist_ok=True)
        environment[environment_keys[field]] = str(path)
    return paths


def test_runtime_settings_require_six_physically_distinct_role_paths(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    expected = _use_six_role_paths(environment, tmp_path)

    settings = load_runtime_settings(environment)

    for field, path in expected.items():
        assert getattr(settings, field) == path
    assert len({getattr(settings, field) for field in expected}) == 6
    assert not hasattr(settings, "registry_directory")
    assert not hasattr(settings, "socket_directory")


def test_runtime_settings_default_to_current_user_plugin_directories(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    hermes_home = tmp_path / "hermes-home"
    environment["HERMES_HOME"] = str(hermes_home)
    temporary_root = Path("/tmp").resolve(strict=True)
    uid = os.getuid()

    settings = load_runtime_settings(environment)

    assert settings.local_gateway_registry_directory == (
        hermes_home / "runtime" / "local-gateways"
    )
    assert settings.local_gateway_socket_directory == (
        temporary_root / f"hermes-local-gateway-{uid}"
    )
    assert settings.control_registry_directory == (
        hermes_home / "runtime" / "control-gateways"
    )
    assert settings.control_socket_directory == (
        temporary_root / f"hermes-control-{uid}"
    )
    assert settings.observer_registry_directory == (
        hermes_home / "runtime" / "observer-gateways"
    )
    assert settings.observer_socket_directory == (
        temporary_root / f"hermes-observer-{uid}"
    )


def test_runtime_settings_default_socket_paths_use_effective_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    environment["HERMES_HOME"] = str(tmp_path / "hermes-home")
    monkeypatch.setattr(settings_module.os, "getuid", lambda: 111)
    monkeypatch.setattr(settings_module.os, "geteuid", lambda: 222)

    settings = load_runtime_settings(environment)

    assert settings.local_gateway_socket_directory.name.endswith("-222")
    assert settings.control_socket_directory.name.endswith("-222")
    assert settings.observer_socket_directory.name.endswith("-222")


def test_runtime_settings_default_registry_follows_profile_plugin_root(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    hermes_root = tmp_path / "hermes-root"
    environment["HERMES_HOME"] = str(hermes_root / "profiles" / "default")

    settings = load_runtime_settings(environment)

    assert settings.local_gateway_registry_directory == (
        hermes_root / "runtime" / "local-gateways"
    )
    assert settings.control_registry_directory == (
        hermes_root / "runtime" / "control-gateways"
    )
    assert settings.observer_registry_directory == (
        hermes_root / "runtime" / "observer-gateways"
    )


@pytest.mark.parametrize(
    ("environment_key", "field"),
    (
        (
            "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
            "local_gateway_registry_directory",
        ),
        (
            "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
            "local_gateway_socket_directory",
        ),
        (
            "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
            "control_registry_directory",
        ),
        ("HERMES_CONNECTOR_CONTROL_SOCKET_DIR", "control_socket_directory"),
        (
            "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
            "observer_registry_directory",
        ),
        ("HERMES_CONNECTOR_OBSERVER_SOCKET_DIR", "observer_socket_directory"),
    ),
)
def test_runtime_settings_preserve_each_explicit_gateway_path_and_default_the_rest(
    tmp_path: Path,
    environment_key: str,
    field: str,
) -> None:
    environment = _valid_environment(tmp_path)
    gateway_environment_keys = (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    )
    for key in gateway_environment_keys:
        environment.pop(key)
    hermes_root = tmp_path / "hermes-root"
    environment["HERMES_HOME"] = str(hermes_root / "profiles" / "default")
    explicit_path = tmp_path / f"explicit-{field}"
    environment[environment_key] = str(explicit_path)
    temporary_root = Path("/tmp").resolve(strict=True)
    uid = os.getuid()
    expected = {
        "local_gateway_registry_directory": (
            hermes_root / "runtime" / "local-gateways"
        ),
        "local_gateway_socket_directory": (
            temporary_root / f"hermes-local-gateway-{uid}"
        ),
        "control_registry_directory": (hermes_root / "runtime" / "control-gateways"),
        "control_socket_directory": temporary_root / f"hermes-control-{uid}",
        "observer_registry_directory": (hermes_root / "runtime" / "observer-gateways"),
        "observer_socket_directory": temporary_root / f"hermes-observer-{uid}",
    }
    expected[field] = explicit_path

    settings = load_runtime_settings(environment)

    for expected_field, expected_path in expected.items():
        assert getattr(settings, expected_field) == expected_path


def test_runtime_settings_rejects_unsafe_default_plugin_home_before_profile_fold(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    environment["HERMES_HOME"] = os.fspath(tmp_path / "hermes-root" / "profiles" / "..")

    with pytest.raises(RuntimeConfigurationError, match="canonical"):
        load_runtime_settings(environment)


def test_runtime_settings_rejects_current_user_tilde_hermes_home_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    fake_user_home = tmp_path / "user-home"
    fake_user_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_user_home))
    environment["HERMES_HOME"] = "~/plugin-home"
    before = set(tmp_path.rglob("*"))

    with pytest.raises(
        RuntimeConfigurationError,
        match="HERMES_HOME must be an absolute path",
    ):
        load_runtime_settings(environment)

    assert set(tmp_path.rglob("*")) == before
    assert (fake_user_home / "plugin-home").exists() is False


@pytest.mark.parametrize(
    "raw_home",
    (
        "~hermes_connector_missing_user_9a8b7c6d/plugin-home",
        "relative/plugin-home",
    ),
)
def test_runtime_settings_rejects_unresolved_or_relative_hermes_home_without_side_effects(
    tmp_path: Path,
    raw_home: str,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    environment["HERMES_HOME"] = raw_home
    before = set(tmp_path.rglob("*"))

    with pytest.raises(
        RuntimeConfigurationError,
        match="HERMES_HOME must be an absolute path",
    ):
        load_runtime_settings(environment)

    assert set(tmp_path.rglob("*")) == before


def test_runtime_settings_rejects_non_string_hermes_home_without_side_effects(
    tmp_path: Path,
) -> None:
    environment: dict[str, object] = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    ):
        environment.pop(key)
    environment["HERMES_HOME"] = object()
    before = set(tmp_path.rglob("*"))

    with pytest.raises(
        RuntimeConfigurationError,
        match="HERMES_HOME must be a path",
    ):
        load_runtime_settings(environment)  # type: ignore[arg-type]

    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "legacy_key",
    ("HERMES_CONNECTOR_REGISTRY_DIR", "HERMES_CONNECTOR_SOCKET_DIR"),
)
def test_runtime_settings_reject_legacy_generic_gateway_paths(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    environment = _valid_environment(tmp_path)
    _use_six_role_paths(environment, tmp_path)
    environment[legacy_key] = str(tmp_path / "legacy-mixed-role-path")

    with pytest.raises(RuntimeConfigurationError, match="legacy generic gateway paths"):
        load_runtime_settings(environment)


def test_runtime_settings_reject_role_path_collision(tmp_path: Path) -> None:
    environment = _valid_environment(tmp_path)
    environment["HERMES_CONNECTOR_OBSERVER_SOCKET_DIR"] = environment[
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR"
    ]

    with pytest.raises(RuntimeConfigurationError, match="role paths must be distinct"):
        load_runtime_settings(environment)


def test_runtime_settings_rejects_lexical_parent_traversal_in_all_paths(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["HERMES_CONNECTOR_OBSERVER_SOCKET_DIR"] = str(
        tmp_path / "observer-registry" / ".." / "observer-sockets"
    )

    with pytest.raises(RuntimeConfigurationError, match="canonical"):
        load_runtime_settings(environment)


def test_runtime_settings_rejects_precreated_parent_symlink_alias(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    alias = tmp_path / "role-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    environment["HERMES_CONNECTOR_OBSERVER_SOCKET_DIR"] = str(alias / "control-sockets")

    with pytest.raises(RuntimeConfigurationError, match="canonical"):
        load_runtime_settings(environment)


def test_runtime_settings_rejects_over_budget_path_component(tmp_path: Path) -> None:
    environment = _valid_environment(tmp_path)
    environment["HERMES_CONNECTOR_OBSERVER_SOCKET_DIR"] = str(tmp_path / ("x" * 256))

    with pytest.raises(RuntimeConfigurationError, match="budget"):
        load_runtime_settings(environment)


def test_runtime_settings_rejects_case_alias_for_existing_role_directory(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    control = Path(environment["HERMES_CONNECTOR_CONTROL_SOCKET_DIR"])
    case_alias = control.with_name(control.name.upper())
    if not case_alias.exists():
        pytest.skip("filesystem is case-sensitive")
    environment["HERMES_CONNECTOR_OBSERVER_SOCKET_DIR"] = str(case_alias)

    with pytest.raises(RuntimeConfigurationError, match="physically distinct"):
        load_runtime_settings(environment)


def test_json_config_rejects_legacy_generic_gateway_paths(tmp_path: Path) -> None:
    environment = _valid_environment(tmp_path)
    config_file = tmp_path / "connector-config.json"
    config_file.write_text(
        json.dumps({"registry_directory": str(tmp_path / "legacy-registry")}),
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(config_file)

    with pytest.raises(RuntimeConfigurationError, match="legacy generic gateway paths"):
        load_runtime_settings(environment)


def test_runtime_settings_load_only_non_secret_values_and_file_reference(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)

    settings = load_runtime_settings(environment)

    assert settings.cloud_endpoint.startswith("wss://")
    assert settings.cloud_api_endpoint.startswith("https://")
    assert settings.profile == "default"
    assert settings.connector_version == "1.2.3"
    assert not hasattr(settings, "runtime_generation")
    assert settings.credential_store == "file"
    assert settings.token_file == Path(environment["HERMES_CONNECTOR_TOKEN_FILE"])
    assert settings.instance_state_file == settings.state_directory / "instances.json"
    assert "opaque-token" not in repr(settings)


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("HERMES_CONNECTOR_CLOUD_ENDPOINT", "ws://cloud.example.test/ws"),
        ("HERMES_CONNECTOR_CLOUD_ENDPOINT", "https://cloud.example.test/ws"),
        ("HERMES_CONNECTOR_VERSION", "latest"),
        ("HERMES_CONNECTOR_CREDENTIAL_STORE", "plaintext"),
        ("HERMES_CONNECTOR_DATABASE_FILE", "relative.sqlite3"),
        ("HERMES_CONNECTOR_TOKEN_FILE", "relative-token"),
    ),
)
def test_runtime_settings_reject_unsafe_values(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment[key] = value

    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)


@pytest.mark.parametrize(
    "secret_key",
    (
        "HERMES_CONNECTOR_ACCESS_TOKEN",
        "HERMES_CONNECTOR_TOKEN",
        "HERMES_ACCESS_TOKEN",
    ),
)
def test_runtime_settings_reject_plaintext_token_environment(
    tmp_path: Path,
    secret_key: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment[secret_key] = "must-never-appear"

    with pytest.raises(RuntimeConfigurationError) as caught:
        load_runtime_settings(environment)

    assert "must-never-appear" not in str(caught.value)


def test_runtime_settings_reject_colliding_managed_file_paths(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["HERMES_CONNECTOR_DATABASE_FILE"] = environment[
        "HERMES_CONNECTOR_TOKEN_FILE"
    ]

    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)


def test_non_secret_json_config_can_be_overridden_by_environment(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    config_file = tmp_path / "connector-config.json"
    config_file.write_text(
        json.dumps(
            {
                "cloud_endpoint": environment.pop("HERMES_CONNECTOR_CLOUD_ENDPOINT"),
                "profile": environment.pop("HERMES_CONNECTOR_PROFILE"),
                "connector_version": environment.pop("HERMES_CONNECTOR_VERSION"),
                "local_gateway_registry_directory": environment.pop(
                    "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR"
                ),
                "local_gateway_socket_directory": environment.pop(
                    "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR"
                ),
                "control_registry_directory": environment.pop(
                    "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR"
                ),
                "control_socket_directory": environment.pop(
                    "HERMES_CONNECTOR_CONTROL_SOCKET_DIR"
                ),
                "observer_registry_directory": environment.pop(
                    "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR"
                ),
                "observer_socket_directory": environment.pop(
                    "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR"
                ),
                "state_directory": environment.pop("HERMES_CONNECTOR_STATE_DIR"),
                "database_file": environment.pop("HERMES_CONNECTOR_DATABASE_FILE"),
                "lock_file": environment.pop("HERMES_CONNECTOR_LOCK_FILE"),
                "credential_store": environment.pop(
                    "HERMES_CONNECTOR_CREDENTIAL_STORE"
                ),
                "token_file": environment.pop("HERMES_CONNECTOR_TOKEN_FILE"),
            }
        ),
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(config_file)
    environment["HERMES_CONNECTOR_PROFILE"] = "profile-from-env"

    settings = load_runtime_settings(environment)

    assert settings.profile == "profile-from-env"


def test_runtime_settings_reject_local_runtime_authority_override(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["HERMES_CONNECTOR_RUNTIME_GENERATION"] = "forged-generation"

    with pytest.raises(
        RuntimeConfigurationError,
        match="local runtime authority configuration is forbidden",
    ):
        load_runtime_settings(environment)


def test_json_config_rejects_local_runtime_authority_override(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    config_file = tmp_path / "connector-config.json"
    config_file.write_text(
        json.dumps({"runtime_generation": "forged-generation"}),
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(config_file)

    with pytest.raises(
        RuntimeConfigurationError,
        match="local runtime authority configuration is forbidden",
    ):
        load_runtime_settings(environment)


def test_keychain_is_default_and_rejects_ambiguous_file_reference(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment.pop("HERMES_CONNECTOR_CREDENTIAL_STORE")
    environment.pop("HERMES_CONNECTOR_TOKEN_FILE")

    settings = load_runtime_settings(environment)

    assert settings.credential_store == "keychain"
    assert not hasattr(settings, "tenant_id")
    assert not hasattr(settings, "device_id")
    assert settings.token_file is None
    assert "cloud-token" not in repr(settings)

    environment["HERMES_CONNECTOR_TOKEN_FILE"] = str(tmp_path / "cloud-token")
    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)


@pytest.mark.parametrize(
    "forbidden_key",
    ("HERMES_CONNECTOR_TENANT_ID", "HERMES_CONNECTOR_DEVICE_ID"),
)
def test_all_modes_reject_self_asserted_tenant_or_device(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment[forbidden_key] = "self-asserted"

    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)


def test_file_reference_requires_explicit_migration_mode_and_path(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment.pop("HERMES_CONNECTOR_TOKEN_FILE")

    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)


def test_json_config_rejects_secret_material_without_disclosure(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    config_file = tmp_path / "connector-config.json"
    config_file.write_text(
        '{"access_token":"must-never-appear"}',
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(config_file)

    with pytest.raises(RuntimeConfigurationError) as caught:
        load_runtime_settings(environment)

    assert "must-never-appear" not in str(caught.value)


def test_json_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    environment = _valid_environment(tmp_path)
    config_file = tmp_path / "connector-config.json"
    config_file.write_text(
        '{"profile":"first","profile":"second"}',
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(config_file)

    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)


@pytest.mark.asyncio
async def test_token_provider_reads_strict_credential_file_without_repr_leak(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "cloud-token"
    token_file.write_text("opaque-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    provider = MacOSFileCloudTokenProvider(token_file)

    assert await provider.access_token() == "opaque-token"
    assert "opaque-token" not in repr(provider)
    await provider.clear_access_token()
    with pytest.raises(UnsafeCredentialFile):
        await provider.access_token()


def test_migration_reference_check_does_not_read_or_parse_legacy_token(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "cloud-token"
    token_file.write_text("expired legacy token with spaces", encoding="utf-8")
    token_file.chmod(0o600)
    provider = MacOSFileCloudTokenProvider(token_file)

    provider.check_reference()

    with pytest.raises(UnsafeCredentialFile):
        provider.check()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_kind", ("relative", "empty", "mode", "symlink"))
async def test_token_provider_rejects_unsafe_credential_files(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    token_file = tmp_path / "cloud-token"
    token_file.write_text("opaque-token", encoding="utf-8")
    token_file.chmod(0o600)
    configured_path: Path = token_file
    if unsafe_kind == "relative":
        configured_path = Path("cloud-token")
    elif unsafe_kind == "empty":
        token_file.write_text("", encoding="utf-8")
    elif unsafe_kind == "mode":
        token_file.chmod(0o640)
    else:
        symlink = tmp_path / "cloud-token-link"
        symlink.symlink_to(token_file)
        configured_path = symlink

    provider = MacOSFileCloudTokenProvider(configured_path)
    with pytest.raises(UnsafeCredentialFile) as caught:
        await provider.access_token()

    assert "opaque-token" not in str(caught.value)


class _MemorySecretStore:
    def __init__(self) -> None:
        self.secret: bytes | None = None
        self.deleted = 0

    def check_available(self) -> None:
        return None

    async def read_secret(self) -> bytes | None:
        return self.secret

    async def create_secret(self, secret: bytes) -> bool:
        if self.secret is not None:
            return False
        self.secret = secret
        return True

    async def write_secret(self, secret: bytes) -> None:
        self.secret = secret

    async def delete_secret(self) -> bool:
        if self.secret is None:
            return False
        self.secret = None
        self.deleted += 1
        return True

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        if self.secret is None:
            return False
        if hashlib.sha256(self.secret).digest() != expected_sha256:
            return False
        self.secret = None
        self.deleted += 1
        return True

    def __repr__(self) -> str:
        return "_MemorySecretStore(<redacted>)"


class _UnexpectedFailureSecretStore(_MemorySecretStore):
    async def read_secret(self) -> bytes | None:
        raise RuntimeError("provider-secret-must-not-escape")


@pytest.mark.asyncio
async def test_keychain_cloud_token_provider_persists_reads_and_deletes() -> None:
    store = _MemorySecretStore()
    provider = MacOSKeychainCloudTokenProvider(store)

    await provider.store_access_token("opaque-keychain-token")
    assert await provider.access_token() == "opaque-keychain-token"
    assert "opaque-keychain-token" not in repr(provider)
    await provider.clear_access_token()

    assert store.secret is None
    assert store.deleted == 1
    with pytest.raises(CloudCredentialUnavailable):
        await provider.access_token()


@pytest.mark.asyncio
async def test_keychain_cloud_token_boundary_redacts_unexpected_store_error() -> None:
    provider = MacOSKeychainCloudTokenProvider(_UnexpectedFailureSecretStore())

    with pytest.raises(CloudCredentialUnavailable) as caught:
        await provider.access_token()

    assert "provider-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    (
        "",
        " leading",
        "trailing ",
        "contains whitespace",
        "contains\nnewline",
    ),
)
async def test_keychain_cloud_token_provider_rejects_invalid_token_before_store(
    token: str,
) -> None:
    store = _MemorySecretStore()
    provider = MacOSKeychainCloudTokenProvider(store)

    with pytest.raises(CloudCredentialUnavailable):
        await provider.store_access_token(token)

    assert store.secret is None


def test_config_file_must_be_absolute_regular_nonsymlink_and_private(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    config_file = tmp_path / "connector-config.json"
    config_file.write_text("{}", encoding="utf-8")
    config_file.chmod(0o644)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(config_file)

    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)

    config_file.chmod(0o600)
    symlink = tmp_path / "connector-config-link"
    symlink.symlink_to(config_file)
    environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(symlink)
    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)

    environment["HERMES_CONNECTOR_CONFIG_FILE"] = os.path.relpath(
        config_file,
        Path.cwd(),
    )
    with pytest.raises(RuntimeConfigurationError):
        load_runtime_settings(environment)
