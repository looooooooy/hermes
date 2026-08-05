"""macOS Local Gateway transport migration tests."""

from __future__ import annotations

import stat
import tempfile
import time
from pathlib import Path

from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2


def test_macos_transport_is_canonical_and_preserves_private_resources() -> None:
    module_path = (
        Path(__file__).parents[3]
        / "src/hermes_agent_plugin/adapters/platform/macos"
        / "local_gateway_transport.py"
    )
    assert module_path.is_file(), "canonical macOS transport is missing"

    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        LOCAL_GATEWAY_AVAILABLE,
        MacOSLocalGatewayResource,
        _MacOSLocalGatewaySettings,
    )

    with tempfile.TemporaryDirectory(prefix="hap-", dir="/tmp") as raw_root:
        root = Path(raw_root)
        settings = _MacOSLocalGatewaySettings(
            profile="default",
            registry_directory=root / "registry",
            socket_directory=root / "sockets",
            authority=runtime_authority_v2(runtime_generation="runtime-1"),
        )
        resource = MacOSLocalGatewayResource(
            settings=settings,
            hello_handler=lambda _raw: "{}",
        )

        resource.start(time.monotonic() + 1.0)

        assert LOCAL_GATEWAY_AVAILABLE is True
        assert resource.name == "macos-local-gateway"
        assert stat.S_IMODE(resource.descriptor_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(resource.socket_path.stat().st_mode) == 0o600

        resource.stop(time.monotonic() + 1.0)
        assert resource.descriptor_path.exists() is False
        assert resource.socket_path.exists() is False
