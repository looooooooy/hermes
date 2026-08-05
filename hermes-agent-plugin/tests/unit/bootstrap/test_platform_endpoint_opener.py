"""Production assembly tests for Host SPI local endpoint openers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_agent_plugin.bootstrap import platform_adapters


class _Registration:
    def close(self) -> None:
        return None


class _HostAuthority:
    def __init__(self) -> None:
        self.profile = "default"
        self.host_bundle_id = "ai.hermes.agent"
        self.bound_generations: list[str] = []

    def bind_runtime(self, runtime_generation: str) -> object:
        self.bound_generations.append(runtime_generation)
        return SimpleNamespace(
            runtime_generation=runtime_generation,
            instance_id="11111111-1111-4111-8111-111111111111",
        )


class _Backend:
    def __init__(self) -> None:
        self.local_gateway_calls: list[dict[str, object]] = []
        self.observer_calls: list[dict[str, object]] = []
        self.control_calls: list[dict[str, object]] = []

    def start_local_gateway_endpoint(self, **kwargs: object) -> _Registration:
        self.local_gateway_calls.append(dict(kwargs))
        return _Registration()

    def start_observer_endpoint(self, **kwargs: object) -> _Registration:
        self.observer_calls.append(dict(kwargs))
        return _Registration()

    def start_control_endpoint(self, **kwargs: object) -> _Registration:
        self.control_calls.append(dict(kwargs))
        return _Registration()


def test_macos_endpoint_opener_reuses_one_host_authority_for_all_roles() -> None:
    backend = _Backend()
    authority = _HostAuthority()
    captures: list[tuple[str, str]] = []

    def capture_authority(*, profile: str, host_bundle_id: str) -> _HostAuthority:
        captures.append((profile, host_bundle_id))
        return authority

    opener = platform_adapters.create_macos_endpoint_opener(
        backend=backend,
        host_authority_factory=capture_authority,
    )
    runtime = SimpleNamespace(
        profile="default",
        runtime_generation="generation-1",
        host_bundle_id="ai.hermes.agent",
        state="ready",
    )
    observer = SimpleNamespace(
        connection_role="observer",
        handle_observer_request=lambda *_args: None,
        transport_disconnected=lambda *_args: None,
    )
    control = SimpleNamespace(
        connection_role="control",
        handle_control_request=lambda *_args: None,
        transport_disconnected=lambda *_args: None,
    )
    local_gateway = SimpleNamespace(
        connection_role="local-gateway",
        handle_local_hello=lambda *_args: "{}",
    )

    opener(local_gateway, runtime)
    opener(observer, runtime)
    opener(control, runtime)

    assert captures == [("default", "ai.hermes.agent")]
    assert authority.bound_generations == ["generation-1"]
    runtime_authority = backend.local_gateway_calls[0]["authority"]
    assert runtime_authority is backend.observer_calls[0]["authority"]
    assert runtime_authority is backend.control_calls[0]["authority"]
    assert backend.local_gateway_calls[0]["hello_handler"] is (
        local_gateway.handle_local_hello
    )
    assert "dispatcher" not in backend.observer_calls[0]
    assert "dispatch" not in backend.control_calls[0]


def test_macos_endpoint_opener_rejects_runtime_identity_change_before_open() -> None:
    backend = _Backend()
    authority = _HostAuthority()
    opener = platform_adapters.create_macos_endpoint_opener(
        backend=backend,
        host_authority_factory=lambda **_kwargs: authority,
    )
    endpoint = SimpleNamespace(
        connection_role="control",
        handle_control_request=lambda *_args: None,
        transport_disconnected=lambda *_args: None,
    )
    first_runtime = SimpleNamespace(
        profile="default",
        runtime_generation="generation-1",
        host_bundle_id="ai.hermes.agent",
        state="ready",
    )
    changed_runtime = SimpleNamespace(
        profile="other",
        runtime_generation="generation-2",
        host_bundle_id="ai.hermes.agent",
        state="ready",
    )

    opener(endpoint, first_runtime)
    with pytest.raises(RuntimeError, match="identity changed"):
        opener(endpoint, changed_runtime)

    assert len(backend.control_calls) == 1
