from __future__ import annotations

from collections.abc import Callable

import pytest

from hermes_agent_plugin.adapters.host.extension import HermesAgentPluginExtension
from hermes_agent_plugin.adapters.host.managed_extension import (
    ManagedRuntimeHermesAgentPluginExtension,
)
from hermes_agent_plugin.adapters.host.spi_v1 import HostSpiFactories


class _Registration:
    def __init__(self, name: str, closed: list[str]) -> None:
        self._name = name
        self._closed = closed

    def close(self) -> None:
        self._closed.append(self._name)


class _Host:
    host_api_version = 1

    def runtime_descriptor(self) -> dict[str, object]:
        return {
            "profile": "default",
            "runtime_generation": "generation-42",
            "state": "ready",
        }

    def session_catalog(self, _request: object) -> dict[str, object]:
        return {
            "profile": "default",
            "runtime_generation": "generation-42",
            "catalog_revision": 7,
            "sessions": (),
            "next_cursor": None,
        }

    def control_snapshot(self, _scope: object) -> object:
        raise AssertionError("empty catalog must not read a session snapshot")


def _factories(*, catalog: bool = True) -> HostSpiFactories:
    return HostSpiFactories(
        observer_request=lambda **value: value,
        session_catalog_request=(lambda **value: value) if catalog else None,
        control_scope=lambda **value: value,
        owner_action_request=lambda **value: value,
        safe_audit_event=lambda **value: value,
    )


def test_starts_relay_after_primary_and_closes_it_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    primary = _Registration("primary", closed)
    relay = _Registration("relay", closed)
    captured_provider: Callable[[], object] | None = None

    monkeypatch.setattr(
        HermesAgentPluginExtension,
        "install",
        lambda _self, _host: primary,
    )

    def opener(provider: Callable[[], object]) -> object:
        nonlocal captured_provider
        captured_provider = provider
        return relay

    extension = ManagedRuntimeHermesAgentPluginExtension(
        host_spi_factories=_factories(),
        update_safety_opener=opener,
    )
    registration = extension.install(_Host())

    assert captured_provider is not None
    assert captured_provider().payload() == {
        "schema_version": 1,
        "profile": "default",
        "runtime_generation": "generation-42",
        "active_tasks": 0,
        "pending_approvals": 0,
        "pending_clarifications": 0,
        "evidence_complete": True,
    }

    registration.close()
    assert closed == ["relay", "primary"]


def test_relay_start_failure_rolls_back_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    primary = _Registration("primary", closed)
    monkeypatch.setattr(
        HermesAgentPluginExtension,
        "install",
        lambda _self, _host: primary,
    )

    def opener(_provider: Callable[[], object]) -> object:
        raise RuntimeError("bind failed")

    extension = ManagedRuntimeHermesAgentPluginExtension(
        host_spi_factories=_factories(),
        update_safety_opener=opener,
    )

    with pytest.raises(RuntimeError, match="bind failed"):
        extension.install(_Host())
    assert closed == ["primary"]


def test_old_host_without_catalog_keeps_primary_and_never_opens_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    primary = _Registration("primary", closed)
    monkeypatch.setattr(
        HermesAgentPluginExtension,
        "install",
        lambda _self, _host: primary,
    )
    called = False

    def opener(_provider: Callable[[], object]) -> object:
        nonlocal called
        called = True
        return _Registration("relay", closed)

    extension = ManagedRuntimeHermesAgentPluginExtension(
        host_spi_factories=_factories(catalog=False),
        update_safety_opener=opener,
    )

    registration = extension.install(_Host())
    assert registration is primary
    assert called is False
