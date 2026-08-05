"""Opaque Plugin adapter for the frozen Hermes Gateway Extension Host SPI v1."""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

HOST_API_VERSION = 1
OWNER_ACTION_METHODS = frozenset(
    {
        "approval.respond",
        "clarify.respond",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
    }
)
OWNER_ACTION_STATUSES = frozenset({"accepted", "rejected", "effect_unknown"})


def required_text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"{field_name} must be canonical text")
    return value


def frozen_json_mapping(
    value: object,
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    copied = copy.deepcopy(dict(value))
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
    return MappingProxyType(copied)


class Registration(Protocol):
    def close(self) -> None: ...


class PreparedObserver(Protocol):
    """Bounded pre-activation observer resource with an immutable snapshot."""

    @property
    def snapshot(self) -> object: ...

    @property
    def activation_deadline_monotonic(self) -> float: ...

    def activate(self) -> Registration: ...

    def close(self) -> None: ...


class GatewayExtensionHostV1(Protocol):
    host_api_version: Literal[1]

    def runtime_descriptor(self) -> object: ...

    def register_local_endpoint(self, endpoint: object) -> Registration: ...

    def prepare_observer(
        self,
        request: object,
        sink: object,
    ) -> PreparedObserver: ...

    def control_snapshot(self, scope: object) -> object: ...

    def invoke_owner_action(self, request: object) -> object: ...

    def add_runtime_listener(self, listener: object) -> Registration: ...

    def audit(self, event: object) -> None: ...


class PublicHostSpiContractUnavailable(RuntimeError):
    """The compatible context did not publish its frozen public DTO module."""


@dataclass(frozen=True)
class HostSpiFactories:
    """Construct only public Host DTOs at the production boundary."""

    observer_request: Callable[..., object]
    session_catalog_request: Callable[..., object] | None
    control_scope: Callable[..., object]
    owner_action_request: Callable[..., object]
    safe_audit_event: Callable[..., object]


_PUBLIC_DTO_CONTRACTS = MappingProxyType(
    {
        "ObserverRequest": (
            ("profile", "durable_session_key", "runtime_generation"),
            ("observer_contract", "required_capabilities"),
            {
                "profile": "contract-probe",
                "durable_session_key": "contract-probe",
                "runtime_generation": "contract-probe",
                "observer_contract": 1,
                "required_capabilities": frozenset(),
            },
        ),
        "SessionCatalogRequest": (
            ("profile", "runtime_generation"),
            ("page_size", "cursor"),
            {
                "profile": "contract-probe",
                "runtime_generation": "contract-probe",
                "page_size": 128,
                "cursor": None,
            },
        ),
        "ControlScope": (
            ("profile", "durable_session_key", "runtime_generation"),
            (),
            {
                "profile": "contract-probe",
                "durable_session_key": "contract-probe",
                "runtime_generation": "contract-probe",
            },
        ),
        "OwnerActionRequest": (
            (
                "profile",
                "durable_session_key",
                "runtime_generation",
                "command_id",
                "method",
                "payload",
            ),
            (),
            {
                "profile": "contract-probe",
                "durable_session_key": "contract-probe",
                "runtime_generation": "contract-probe",
                "command_id": "contract-probe",
                "method": "prompt.submit",
                "payload": {},
            },
        ),
        "SafeAuditEvent": (
            ("name", "profile", "runtime_generation"),
            ("attributes",),
            {
                "name": "runtime.lifecycle",
                "profile": "contract-probe",
                "runtime_generation": "contract-probe",
                "attributes": {"action": "started", "state": "ready"},
            },
        ),
    }
)


def _validated_public_dto_constructor(module: object, name: str) -> type[object]:
    constructor = getattr(module, name)
    required, optional, probe = _PUBLIC_DTO_CONTRACTS[name]
    expected_names = (*required, *optional)
    if (
        not isinstance(constructor, type)
        or constructor.__module__ != "hermes_cli.extension_host_v1"
        or constructor.__name__ != name
        or not is_dataclass(constructor)
        or not getattr(constructor, "__dataclass_params__").frozen
        or tuple(item.name for item in fields(constructor)) != expected_names
    ):
        raise TypeError("public DTO constructor is malformed")
    parameters = tuple(inspect.signature(constructor).parameters.values())
    if (
        tuple(parameter.name for parameter in parameters) != expected_names
        or any(
            parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        )
        or tuple(
            parameter.default is inspect.Parameter.empty for parameter in parameters
        )
        != (*([True] * len(required)), *([False] * len(optional)))
    ):
        raise TypeError("public DTO constructor is malformed")
    dataclass_fields = {item.name: item for item in fields(constructor)}
    if name == "ObserverRequest":
        observer_contract = dataclass_fields["observer_contract"]
        required_capabilities = dataclass_fields["required_capabilities"]
        if (
            observer_contract.default != 1
            or observer_contract.default_factory is not MISSING
            or required_capabilities.default is not MISSING
            or required_capabilities.default_factory is not frozenset
        ):
            raise TypeError("public DTO constructor is malformed")
    elif name == "SessionCatalogRequest":
        page_size = dataclass_fields["page_size"]
        cursor = dataclass_fields["cursor"]
        if (
            page_size.default != 64
            or page_size.default_factory is not MISSING
            or cursor.default is not None
            or cursor.default_factory is not MISSING
        ):
            raise TypeError("public DTO constructor is malformed")
    elif name == "SafeAuditEvent":
        attributes = dataclass_fields["attributes"]
        if attributes.default is not MISSING or attributes.default_factory is not dict:
            raise TypeError("public DTO constructor is malformed")
    instance = constructor(**probe)
    if type(instance) is not constructor:
        raise TypeError("public DTO constructor is malformed")
    if name == "ObserverRequest":
        defaults = constructor(
            profile="contract-probe",
            durable_session_key="contract-probe",
            runtime_generation="contract-probe",
        )
        explicit_v2 = constructor(
            profile="contract-probe",
            durable_session_key="contract-probe",
            runtime_generation="contract-probe",
            observer_contract=2,
            required_capabilities=frozenset({"session.observe.output-parity.v1"}),
        )
        if (
            defaults.observer_contract != 1
            or type(defaults.required_capabilities) is not frozenset
            or defaults.required_capabilities != frozenset()
            or explicit_v2.observer_contract != 2
            or explicit_v2.required_capabilities
            != frozenset({"session.observe.output-parity.v1"})
        ):
            raise TypeError("public DTO constructor is malformed")
    elif name == "SessionCatalogRequest":
        defaults = constructor(
            profile="contract-probe",
            runtime_generation="contract-probe",
        )
        if defaults.page_size != 64 or defaults.cursor is not None:
            raise TypeError("public DTO constructor is malformed")
    return constructor


def load_public_host_spi_factories() -> HostSpiFactories:
    """Load exact DTO constructors from the stable Core public SPI module."""

    try:
        module = importlib.import_module("hermes_cli.extension_host_v1")
        version = getattr(module, "GATEWAY_EXTENSION_SPI_VERSION")
        if type(version) is not int or version != HOST_API_VERSION:
            raise PublicHostSpiContractUnavailable(
                "Hermes public Host SPI v1 DTO module is unavailable"
            )
        factories = HostSpiFactories(
            observer_request=_validated_public_dto_constructor(
                module,
                "ObserverRequest",
            ),
            session_catalog_request=(
                _validated_public_dto_constructor(
                    module,
                    "SessionCatalogRequest",
                )
                if hasattr(module, "SessionCatalogRequest")
                else None
            ),
            control_scope=_validated_public_dto_constructor(module, "ControlScope"),
            owner_action_request=_validated_public_dto_constructor(
                module,
                "OwnerActionRequest",
            ),
            safe_audit_event=_validated_public_dto_constructor(
                module,
                "SafeAuditEvent",
            ),
        )
    except PublicHostSpiContractUnavailable:
        raise
    except Exception as error:
        raise PublicHostSpiContractUnavailable(
            "Hermes public Host SPI v1 DTO module is unavailable"
        ) from error
    return factories


class CompositeRegistration:
    """Close registrations once, in reverse order, without skipping failures."""

    def __init__(
        self,
        registrations: Sequence[Registration],
        *,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self._registrations = tuple(registrations)
        self._pending = list(registrations)
        self._on_closed = on_closed
        self._lock = threading.RLock()
        self._closed = False
        self._on_closed_done = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            first_error: BaseException | None = None
            for registration in reversed(tuple(self._pending)):
                try:
                    registration.close()
                except BaseException as error:  # noqa: BLE001
                    first_error = first_error or error
                else:
                    self._pending.remove(registration)
            if not self._pending and not self._on_closed_done:
                try:
                    if self._on_closed is not None:
                        self._on_closed()
                except BaseException as error:  # noqa: BLE001
                    first_error = first_error or error
                else:
                    self._on_closed_done = True
            if first_error is not None:
                raise first_error
            self._closed = True


__all__ = [
    "HOST_API_VERSION",
    "OWNER_ACTION_METHODS",
    "OWNER_ACTION_STATUSES",
    "CompositeRegistration",
    "GatewayExtensionHostV1",
    "HostSpiFactories",
    "PreparedObserver",
    "PublicHostSpiContractUnavailable",
    "Registration",
    "frozen_json_mapping",
    "load_public_host_spi_factories",
    "required_text",
]
