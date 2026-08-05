"""Fail-closed Linux Local Gateway availability."""

import pytest

from hermes_agent_plugin.adapters.platform.capabilities import (
    PlatformLocalGatewayUnavailable,
)
from hermes_agent_plugin.adapters.platform.linux import (
    LOCAL_GATEWAY_AVAILABLE,
    LOCAL_GATEWAY_CAPABILITIES,
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.linux.local_relay import (
    create_local_relay_backend,
)


def test_linux_local_gateway_is_unavailable_and_fails_closed() -> None:
    assert LOCAL_GATEWAY_AVAILABLE is False
    assert LOCAL_GATEWAY_CAPABILITIES.features == frozenset()
    with pytest.raises(
        PlatformLocalGatewayUnavailable,
        match="linux_local_gateway_not_implemented",
    ):
        create_local_gateway_resource(
            settings=object(),
            hello_handler=lambda _raw: "{}",
            ready=lambda: True,
            clock=lambda: 1.0,
        )


def test_linux_local_relays_are_unavailable_and_fail_closed() -> None:
    backend = create_local_relay_backend()

    with pytest.raises(
        PlatformLocalGatewayUnavailable,
        match="linux_local_relay_not_implemented",
    ):
        backend.list_control_endpoints()
    with pytest.raises(
        PlatformLocalGatewayUnavailable,
        match="linux_local_relay_not_implemented",
    ):
        backend.list_observer_endpoints()
