from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from hermes_agent_plugin.adapters.host.extension import _LocalEndpointLifecycle
from hermes_agent_plugin.adapters.platform.windows.local_relay import (
    create_local_relay_backend,
)
from hermes_agent_plugin.bootstrap.platform_adapters import (
    create_windows_endpoint_opener,
)

from hermes_connector.adapters.platform.windows.named_pipe import (
    connect_same_user_pipe,
    profile_pipe_name,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")


def _runtime(profile: str) -> SimpleNamespace:
    return SimpleNamespace(
        profile=profile,
        runtime_generation=f"generation-{profile}-1",
        host_bundle_id="com.hermes.windows-host-lifecycle-test",
        state="ready",
    )


def _endpoints() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    gateway = SimpleNamespace(
        connection_role="local-gateway",
        handle_local_hello=lambda _raw: b"{}",
    )
    observer = SimpleNamespace(
        connection_role="observer",
        observer_contract=1,
        handle_observer_request=lambda _request, _transport: None,
        transport_disconnected=lambda _transport: None,
    )
    control = SimpleNamespace(
        connection_role="control",
        handle_control_request=lambda _request, _transport: None,
        transport_disconnected=lambda _transport: None,
    )
    return gateway, observer, control


class _Host:
    def __init__(self, opener, runtime: object) -> None:
        self._opener = opener
        self._runtime = runtime

    def register_local_endpoint(self, endpoint: object):
        return self._opener(endpoint, self._runtime)


class _FailControlBackend:
    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def start_local_gateway_endpoint(self, **kwargs: object):
        return self._delegate.start_local_gateway_endpoint(**kwargs)

    def start_observer_endpoint(self, **kwargs: object):
        return self._delegate.start_observer_endpoint(**kwargs)

    def start_control_endpoint(self, **_kwargs: object):
        raise RuntimeError("injected control start failure")

    def list_observer_endpoints(self):
        return self._delegate.list_observer_endpoints()

    def list_control_endpoints(self):
        return self._delegate.list_control_endpoints()


def _assert_discovery_closed(profile: str) -> None:
    with pytest.raises((OSError, TimeoutError)):
        connect_same_user_pipe(
            profile_pipe_name("discovery", profile),
            timeout_seconds=0.2,
        )


def test_windows_host_lifecycle_opens_and_closes_all_three_roles() -> None:
    profile = "lifecycle"
    backend = create_local_relay_backend()
    opener = create_windows_endpoint_opener(backend=backend)
    lifecycle = _LocalEndpointLifecycle(
        _Host(opener, _runtime(profile)),
        _endpoints(),
    )

    lifecycle.start()
    discovery = connect_same_user_pipe(
        profile_pipe_name("discovery", profile),
        timeout_seconds=1.0,
    )
    discovery.close()
    try:
        observer = backend.list_observer_endpoints()
        control = backend.list_control_endpoints()
        assert len(observer) == 1
        assert len(control) == 1
        assert observer[0].profile == profile
        assert control[0].profile == profile
        assert observer[0].instance_id == control[0].instance_id
    finally:
        lifecycle.close()

    assert backend.list_observer_endpoints() == []
    assert backend.list_control_endpoints() == []
    _assert_discovery_closed(profile)


def test_windows_host_lifecycle_rolls_back_gateway_and_observer_on_control_failure() -> None:
    profile = "rollback"
    delegate = create_local_relay_backend()
    backend = _FailControlBackend(delegate)
    opener = create_windows_endpoint_opener(backend=backend)
    lifecycle = _LocalEndpointLifecycle(
        _Host(opener, _runtime(profile)),
        _endpoints(),
    )

    with pytest.raises(RuntimeError, match="injected control start failure"):
        lifecycle.start()

    assert backend.list_observer_endpoints() == []
    assert backend.list_control_endpoints() == []
    _assert_discovery_closed(profile)
