from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_connector.adapters.platform.windows.private_state import (
    atomic_write_private_file,
    ensure_private_directory,
)
from hermes_connector.bootstrap.settings import (
    RuntimeConfigurationError,
    load_runtime_settings,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows settings required")


def _minimal_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HERMES_CONNECTOR_CLOUD_ENDPOINT": (
            "wss://cloud.example.test/hermes/internal/connector/ws"
        ),
        "HERMES_CONNECTOR_API_ENDPOINT": "https://cloud.example.test/hermes",
        "HERMES_CONNECTOR_PROFILE": "default",
        "HERMES_CONNECTOR_VERSION": "1.2.3",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
    }


def test_windows_settings_default_to_dpapi_and_profile_private_state(
    tmp_path: Path,
) -> None:
    environment = _minimal_environment(tmp_path)
    hermes_home = Path(environment["HERMES_HOME"])

    settings = load_runtime_settings(environment, platform_name="windows")

    expected_state = hermes_home / "connector" / "profiles" / "default" / "state"
    assert settings.credential_store == "dpapi"
    assert settings.token_file is None
    assert settings.state_directory == expected_state
    assert settings.database_file == expected_state / "connector.sqlite3"
    assert settings.lock_file == expected_state / "connector.lock"
    role_paths = {
        settings.local_gateway_registry_directory,
        settings.local_gateway_socket_directory,
        settings.control_registry_directory,
        settings.control_socket_directory,
        settings.observer_registry_directory,
        settings.observer_socket_directory,
    }
    assert len(role_paths) == 6
    assert all(hermes_home / "runtime" / "windows" in path.parents for path in role_paths)
    assert not expected_state.exists()


@pytest.mark.parametrize("store", ["keychain", "file", "unknown"])
def test_windows_settings_reject_non_dpapi_credential_store(
    tmp_path: Path,
    store: str,
) -> None:
    environment = _minimal_environment(tmp_path)
    environment["HERMES_CONNECTOR_CREDENTIAL_STORE"] = store

    with pytest.raises(RuntimeConfigurationError, match="dpapi"):
        load_runtime_settings(environment, platform_name="windows")


def test_windows_settings_reject_plaintext_token_file_even_with_dpapi(
    tmp_path: Path,
) -> None:
    environment = _minimal_environment(tmp_path)
    environment["HERMES_CONNECTOR_CREDENTIAL_STORE"] = "dpapi"
    environment["HERMES_CONNECTOR_TOKEN_FILE"] = str(tmp_path / "token")

    with pytest.raises(RuntimeConfigurationError, match="DPAPI"):
        load_runtime_settings(environment, platform_name="windows")


def test_windows_settings_read_private_config_file_and_keep_defaults(
    tmp_path: Path,
) -> None:
    root = ensure_private_directory(tmp_path / "private-home")
    config_path = root / "connector-config.json"
    payload = {
        "cloud_endpoint": "wss://cloud.example.test/hermes/internal/connector/ws",
        "cloud_api_endpoint": "https://cloud.example.test/hermes",
        "profile": "default",
        "connector_version": "1.2.3",
    }
    atomic_write_private_file(
        config_path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        maximum=65_536,
    )

    settings = load_runtime_settings(
        {
            "HERMES_CONNECTOR_CONFIG_FILE": str(config_path),
            "HERMES_HOME": str(root),
        },
        platform_name="windows",
    )

    assert settings.credential_store == "dpapi"
    assert settings.state_directory == (
        root / "connector" / "profiles" / "default" / "state"
    )


def test_windows_settings_reject_config_from_inherited_default_acl(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "unsafe-config.json"
    config_path.write_text(
        json.dumps(
            {
                "cloud_endpoint": "wss://cloud.example.test/hermes/internal/connector/ws",
                "cloud_api_endpoint": "https://cloud.example.test/hermes",
                "profile": "default",
                "connector_version": "1.2.3",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigurationError, match="configuration file"):
        load_runtime_settings(
            {
                "HERMES_CONNECTOR_CONFIG_FILE": str(config_path),
                "HERMES_HOME": str(tmp_path / "hermes-home"),
            },
            platform_name="windows",
        )
