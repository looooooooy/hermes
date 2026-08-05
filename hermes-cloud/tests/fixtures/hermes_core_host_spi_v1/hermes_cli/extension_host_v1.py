"""Stable public contract for gateway extensions hosted by Hermes Core.

This module is intentionally implementation-free.  Plugins may depend on the
DTOs and protocols below, but never receive private Agent, SessionDB, runner,
queue, transport, port, or authentication objects through this boundary.
Session and command identifiers are sensitive and therefore redacted by default
from public DTO representations alongside payload content.
"""

from __future__ import annotations

import copy
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Protocol

GATEWAY_EXTENSION_SPI_VERSION: Literal[1] = 1
GATEWAY_EXTENSION_CAPABILITIES_V1 = frozenset(
    {
        "audit.safe.v1",
        "extension.lifecycle.v1",
        "runtime.descriptor.v1",
        "session.observe.v1",
        "session.owner-actions.v1",
    }
)
OWNER_ACTION_METHODS_V1 = frozenset(
    {
        "approval.respond",
        "clarify.respond",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
    }
)


class HostSpiError(RuntimeError):
    """Base class for fail-closed, operator-safe Host SPI errors."""


class HostSpiVersionError(HostSpiError):
    def __init__(self, *, expected: int, observed: object) -> None:
        observed_label = str(observed) if type(observed) is int else "invalid"
        super().__init__(
            "gateway extension SPI version mismatch: "
            f"expected {expected}, observed {observed_label}"
        )


class HostSpiCapabilityError(HostSpiError):
    def __init__(self, missing: object) -> None:
        if isinstance(missing, (set, frozenset, list, tuple)):
            names = sorted(
                item
                for item in missing
                if isinstance(item, str) and item and item == item.strip()
            )
        else:
            names = []
        label = ", ".join(names) if names else "invalid capability declaration"
        super().__init__(f"gateway extension capabilities unavailable: {label}")
        self.missing = frozenset(names)


class HostSpiUnavailableError(HostSpiError):
    def __init__(self) -> None:
        super().__init__("gateway extension host is unavailable")


class HostSpiRegistrationError(HostSpiError):
    """Raised when an extension violates the installation contract."""


def _required_text(value: object, field_name: str, *, max_length: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"{field_name} must be canonical text")
    return value


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze_json_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _frozen_json_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    copied = copy.deepcopy(dict(value))
    if any(
        not isinstance(key, str)
        or not key
        or key != key.strip()
        or any(unicodedata.category(character) == "Cc" for character in key)
        for key in copied
    ):
        raise ValueError(f"{field_name} keys must be canonical text")
    try:
        json.dumps(
            copied,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be canonical JSON") from error
    return _freeze_json_value(copied)


def _capability_versions(value: object) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError("capabilities must be an object")
    copied = dict(value)
    if any(
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or any(unicodedata.category(character) == "Cc" for character in name)
        for name in copied
    ):
        raise ValueError("capability names must be canonical text")
    if any(type(version) is not int or version < 0 for version in copied.values()):
        raise ValueError("capability versions must be non-negative integers")
    return MappingProxyType(dict(copied))


@dataclass(frozen=True)
class RuntimeDescriptor:
    profile: str
    runtime_generation: str
    host_bundle_id: str
    state: Literal["ready", "unavailable"]
    capabilities: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.profile, "profile")
        _required_text(self.runtime_generation, "runtime_generation")
        _required_text(self.host_bundle_id, "host_bundle_id")
        if self.state not in {"ready", "unavailable"}:
            raise ValueError("state must be ready or unavailable")
        object.__setattr__(self, "capabilities", _capability_versions(self.capabilities))


@dataclass(frozen=True, repr=False)
class ObserverRequest:
    profile: str
    durable_session_key: str
    runtime_generation: str
    observer_contract: Literal[1, 2] = 1
    required_capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _required_text(self.profile, "profile")
        _required_text(self.durable_session_key, "durable_session_key")
        _required_text(self.runtime_generation, "runtime_generation")
        if self.observer_contract not in {1, 2}:
            raise ValueError("observer_contract must be 1 or 2")
        capabilities = frozenset(self.required_capabilities)
        if any(
            not isinstance(capability, str)
            or not capability
            or capability != capability.strip()
            or any(
                unicodedata.category(character) == "Cc"
                for character in capability
            )
            for capability in capabilities
        ):
            raise ValueError("required_capabilities must be canonical text")
        object.__setattr__(self, "required_capabilities", capabilities)

    def __repr__(self) -> str:
        return (
            "ObserverRequest("
            f"profile={self.profile!r}, durable_session_key=[REDACTED], "
            f"runtime_generation={self.runtime_generation!r}, "
            f"observer_contract={self.observer_contract!r}, "
            f"required_capabilities={self.required_capabilities!r})"
        )


@dataclass(frozen=True, repr=False)
class ObserverEvent:
    profile: str
    durable_session_key: str
    runtime_generation: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        _required_text(self.profile, "profile")
        _required_text(self.durable_session_key, "durable_session_key")
        _required_text(self.runtime_generation, "runtime_generation")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        _required_text(self.event_type, "event_type")
        object.__setattr__(self, "payload", _frozen_json_mapping(self.payload, "payload"))

    def __repr__(self) -> str:
        return (
            "ObserverEvent("
            f"profile={self.profile!r}, durable_session_key=[REDACTED], "
            f"runtime_generation={self.runtime_generation!r}, sequence={self.sequence!r}, "
            f"event_type={self.event_type!r}, payload=[REDACTED])"
        )


@dataclass(frozen=True, repr=False)
class ControlScope:
    profile: str
    durable_session_key: str
    runtime_generation: str

    def __post_init__(self) -> None:
        _required_text(self.profile, "profile")
        _required_text(self.durable_session_key, "durable_session_key")
        _required_text(self.runtime_generation, "runtime_generation")

    def __repr__(self) -> str:
        return (
            "ControlScope("
            f"profile={self.profile!r}, durable_session_key=[REDACTED], "
            f"runtime_generation={self.runtime_generation!r})"
        )


@dataclass(frozen=True, repr=False)
class ControlSnapshot:
    profile: str
    durable_session_key: str
    runtime_generation: str
    control_revision: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.profile, "profile")
        _required_text(self.durable_session_key, "durable_session_key")
        _required_text(self.runtime_generation, "runtime_generation")
        if type(self.control_revision) is not int or self.control_revision < 0:
            raise ValueError("control_revision must be a non-negative integer")
        object.__setattr__(self, "payload", _frozen_json_mapping(self.payload, "payload"))

    def __repr__(self) -> str:
        return (
            "ControlSnapshot("
            f"profile={self.profile!r}, durable_session_key=[REDACTED], "
            f"runtime_generation={self.runtime_generation!r}, "
            f"control_revision={self.control_revision!r}, payload=[REDACTED])"
        )


class OwnerActionStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EFFECT_UNKNOWN = "effect_unknown"


@dataclass(frozen=True, repr=False)
class OwnerActionRequest:
    profile: str
    durable_session_key: str
    runtime_generation: str
    command_id: str
    method: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        _required_text(self.profile, "profile")
        _required_text(self.durable_session_key, "durable_session_key")
        _required_text(self.runtime_generation, "runtime_generation")
        _required_text(self.command_id, "command_id")
        method = _required_text(self.method, "method")
        if method not in OWNER_ACTION_METHODS_V1:
            raise ValueError("owner action method is unavailable")
        object.__setattr__(self, "payload", _frozen_json_mapping(self.payload, "payload"))

    def __repr__(self) -> str:
        return (
            "OwnerActionRequest("
            f"profile={self.profile!r}, durable_session_key=[REDACTED], "
            f"runtime_generation={self.runtime_generation!r}, command_id=[REDACTED], "
            f"method={self.method!r}, payload=[REDACTED])"
        )


@dataclass(frozen=True, repr=False)
class OwnerActionResult:
    status: OwnerActionStatus
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            status = OwnerActionStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid owner action status") from error
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "payload", _frozen_json_mapping(self.payload, "payload"))

    def __repr__(self) -> str:
        return f"OwnerActionResult(status={self.status.value!r}, payload=[REDACTED])"


@dataclass(frozen=True, repr=False)
class SafeAuditEvent:
    name: str
    profile: str
    runtime_generation: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.name, "name")
        _required_text(self.profile, "profile")
        _required_text(self.runtime_generation, "runtime_generation")
        object.__setattr__(
            self,
            "attributes",
            _frozen_json_mapping(self.attributes, "attributes"),
        )

    def __repr__(self) -> str:
        return (
            "SafeAuditEvent("
            f"name={self.name!r}, profile={self.profile!r}, "
            f"runtime_generation={self.runtime_generation!r}, attributes=[REDACTED])"
        )


class Registration(Protocol):
    def close(self) -> None: ...


class PreparedObserver(Protocol):
    @property
    def snapshot(self) -> object: ...

    @property
    def activation_deadline_monotonic(self) -> float: ...

    def activate(self) -> Registration: ...

    def close(self) -> None: ...


class EndpointDescriptor(Protocol):
    @property
    def connection_role(self) -> str: ...


class EventSink(Protocol):
    def __call__(self, event: ObserverEvent) -> None: ...


class RuntimeListener(Protocol):
    def __call__(self, descriptor: RuntimeDescriptor) -> None: ...


class GatewayExtensionV1(Protocol):
    def install(self, host: GatewayExtensionHostV1) -> Registration: ...


class GatewayExtensionHostV1(Protocol):
    host_api_version: Literal[1]

    def runtime_descriptor(self) -> RuntimeDescriptor: ...

    def register_local_endpoint(self, endpoint: EndpointDescriptor) -> Registration: ...

    def prepare_observer(
        self,
        request: ObserverRequest,
        sink: EventSink,
    ) -> PreparedObserver: ...

    def control_snapshot(self, scope: ControlScope) -> ControlSnapshot: ...

    def invoke_owner_action(self, request: OwnerActionRequest) -> OwnerActionResult: ...

    def add_runtime_listener(self, listener: RuntimeListener) -> Registration: ...

    def audit(self, event: SafeAuditEvent) -> None: ...


class GatewayExtensionContextV1(Protocol):
    @property
    def gateway_extension_spi_version(self) -> Literal[1]: ...

    @property
    def gateway_extension_capabilities(self) -> frozenset[str]: ...

    def register_gateway_extension(
        self,
        extension: GatewayExtensionV1,
        *,
        spi_version: Literal[1],
    ) -> Registration: ...


@dataclass(frozen=True)
class ExtensionShutdownFailure:
    plugin_key: str
    error_type: str


__all__ = [
    "GATEWAY_EXTENSION_CAPABILITIES_V1",
    "GATEWAY_EXTENSION_SPI_VERSION",
    "OWNER_ACTION_METHODS_V1",
    "ControlScope",
    "ControlSnapshot",
    "EndpointDescriptor",
    "EventSink",
    "ExtensionShutdownFailure",
    "GatewayExtensionContextV1",
    "GatewayExtensionHostV1",
    "GatewayExtensionV1",
    "HostSpiCapabilityError",
    "HostSpiError",
    "HostSpiRegistrationError",
    "HostSpiUnavailableError",
    "HostSpiVersionError",
    "ObserverEvent",
    "ObserverRequest",
    "OwnerActionRequest",
    "OwnerActionResult",
    "OwnerActionStatus",
    "PreparedObserver",
    "Registration",
    "RuntimeDescriptor",
    "RuntimeListener",
    "SafeAuditEvent",
]
