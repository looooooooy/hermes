"""Test-only Host SPI DTOs and factories; never packaged in the Plugin wheel."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

from hermes_agent_plugin.adapters.host.spi_v1 import HostSpiFactories


@dataclass(frozen=True)
class ObserverRequest:
    profile: str
    durable_session_key: str
    runtime_generation: str
    observer_contract: Literal[1, 2] = 1
    required_capabilities: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class SessionCatalogRequest:
    profile: str
    runtime_generation: str
    page_size: int = 64
    cursor: str | None = None


@dataclass(frozen=True)
class ControlScope:
    profile: str
    durable_session_key: str
    runtime_generation: str


@dataclass(frozen=True)
class OwnerActionRequest:
    profile: str
    durable_session_key: str
    runtime_generation: str
    command_id: str
    method: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class SafeAuditEvent:
    name: str
    profile: str
    runtime_generation: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attributes",
            MappingProxyType(dict(self.attributes)),
        )


TEST_HOST_SPI_FACTORIES = HostSpiFactories(
    observer_request=ObserverRequest,
    session_catalog_request=SessionCatalogRequest,
    control_scope=ControlScope,
    owner_action_request=OwnerActionRequest,
    safe_audit_event=SafeAuditEvent,
)


__all__ = ["TEST_HOST_SPI_FACTORIES"]
