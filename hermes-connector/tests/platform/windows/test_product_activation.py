from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermes_connector import cli
from hermes_connector.adapters.platform.windows import AVAILABILITY
from hermes_connector.bootstrap.platform import select_platform_adapters
from hermes_connector.bootstrap.settings import load_runtime_settings
from hermes_connector.bootstrap.windows_provision import (
    provision_windows_runtime_state,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows activation required")


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HERMES_CONNECTOR_CLOUD_ENDPOINT": "wss://cloud.example.test/connector/ws",
        "HERMES_CONNECTOR_API_ENDPOINT": "https://cloud.example.test/hermes",
        "HERMES_CONNECTOR_PROFILE": "default",
        "HERMES_CONNECTOR_VERSION": "1.2.3",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
    }


def test_windows_availability_and_real_selector_are_enabled() -> None:
    assert AVAILABILITY.available is True
    AVAILABILITY.require_available()

    selected = select_platform_adapters()
    assert selected.platform_name == "windows"
    assert selected.agent_discovery_type.__name__ == "WindowsAgentDiscovery"
    assert selected.local_gateway_transport_type.__name__ == (
        "WindowsLocalGatewayTransport"
    )
    assert selected.instance_lock_type.__name__ == "WindowsInstanceLock"

    explicit = select_platform_adapters("win32")
    assert explicit == selected


def test_windows_selector_and_cli_do_not_import_macos_package(tmp_path: Path) -> None:
    macos_prefix = "hermes_connector.adapters.platform.macos"
    assert not any(
        name == macos_prefix or name.startswith(macos_prefix + ".")
        for name in sys.modules
    )

    environment = _environment(tmp_path)
    settings = load_runtime_settings(environment, platform_name="windows")
    provision_windows_runtime_state(settings)
    lines: list[str] = []

    exit_code = cli.main(
        ["--check"],
        environment=environment,
        output=lines.append,
    )

    assert exit_code == 0
    assert lines == ["hermes-connector: configuration_valid"]
    assert not any(
        name == macos_prefix or name.startswith(macos_prefix + ".")
        for name in sys.modules
    )


def test_windows_real_selector_status_without_receipt_is_not_ready(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    settings = load_runtime_settings(environment, platform_name="windows")
    provision_windows_runtime_state(settings)
    lines: list[str] = []

    exit_code = cli.main(
        ["status", "--json"],
        environment=environment,
        output=lines.append,
    )

    assert exit_code == 3
    assert lines == ['{"ready":false}']
