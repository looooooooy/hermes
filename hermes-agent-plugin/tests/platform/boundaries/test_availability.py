"""Cross-platform truthfulness of Local Gateway availability."""

import importlib

import pytest

from hermes_agent_plugin.adapters.platform.linux import (
    LOCAL_GATEWAY_CAPABILITIES as LINUX_CAPABILITIES,
)
from hermes_agent_plugin.adapters.platform.macos import (
    LOCAL_GATEWAY_CAPABILITIES as MACOS_CAPABILITIES,
)
from hermes_agent_plugin.adapters.platform.windows import (
    LOCAL_GATEWAY_CAPABILITIES as WINDOWS_CAPABILITIES,
)


def test_platform_availability_matrix_matches_verified_implementations() -> None:
    assert MACOS_CAPABILITIES.available is True
    assert MACOS_CAPABILITIES.transport == "unix-domain-socket"
    assert LINUX_CAPABILITIES.available is False
    assert LINUX_CAPABILITIES.transport is None
    assert WINDOWS_CAPABILITIES.available is False
    assert WINDOWS_CAPABILITIES.transport is None


@pytest.mark.parametrize(
    ("platform_name", "canonical_module"),
    (
        (
            "linux",
            "hermes_agent_plugin.adapters.platform.linux",
        ),
        (
            "win32",
            "hermes_agent_plugin.adapters.platform.windows",
        ),
    ),
)
def test_canonical_compatibility_capability_selector_matches_platform(
    platform_name: str,
    canonical_module: str,
    monkeypatch,
) -> None:
    from hermes_agent_plugin.bootstrap import platform_adapters

    canonical = importlib.import_module(canonical_module)
    monkeypatch.setattr(platform_adapters.sys, "platform", platform_name)

    selected = platform_adapters.select_platform_local_gateway_capabilities()

    assert selected is canonical.LOCAL_GATEWAY_CAPABILITIES
