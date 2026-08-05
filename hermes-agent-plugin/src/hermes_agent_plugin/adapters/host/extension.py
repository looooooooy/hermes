"""Hermes Gateway Extension Host SPI v1 adapter."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from ...application.control_dispatcher import (
    ControlRequestDispatcher,
    OwnerActionMethodUnavailable,
)
from ...domain.control_lease import ControlLeaseManager, SessionBindingMismatch
from ...ports.local_relay import observer_endpoint_contract
from ..local_protocol.control_v1 import CONTROL_AVAILABLE_METHODS
from ..local_protocol.handshake_v1 import LocalContractV1Adapter
from ..local_protocol.session_catalog_v1 import (
    SESSION_CATALOG_CAPABILITY,
    SESSION_CATALOG_METHODS,
    SessionCatalogV1Controller,
)
from .observer_v2 import (
    OUTPUT_PARITY_CAPABILITY,
    ObserverV2Bundle,
    ObserverV2Projection,
    ObserverV2Violation,
    load_observer_v2_bundle,
)
from .spi_v1 import (
    HOST_API_VERSION,
    OWNER_ACTION_METHODS,
    OWNER_ACTION_STATUSES,
    CompositeRegistration,
    GatewayExtensionHostV1,
    HostSpiFactories,
    PreparedObserver,
    Registration,
    frozen_json_mapping,
    load_public_host_spi_factories,
    required_text,
)

_OBSERVER_METHODS = frozenset(
    {"session.observe.subscribe", "session.observe.unsubscribe"}
)
_CONTROL_INFRA_METHODS = CONTROL_AVAILABLE_METHODS - OWNER_ACTION_METHODS
_OWNER_SCOPE_FIELDS = frozenset(
    {
        "client_request_id",
        "lease_id",
        "profile",
        "relay_local_only",
        "runtime_generation",
        "session_key",
    }
)
_OBSERVER_V2_COLLECTIONS = ("todo_sections", "subagents", "tools", "terminals")
MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT = 64
MAX_OBSERVER_SUBSCRIPTIONS_TOTAL = 1_024


def _unavailable_endpoint_opener(_endpoint: object, _runtime: object) -> Registration:
    raise RuntimeError("local endpoint opener is unavailable")


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_json_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (bool, int, float, str)):
        return enum_value
    raise TypeError("observer fact is not canonical JSON")


class _ObserverWireSink:
    def __init__(self, transport: object) -> None:
        self._transport = transport

    def __call__(self, event: object) -> None:
        self.on_event(event)

    def on_event(self, event: object) -> None:
        self._write(event)

    def on_snapshot(self, snapshot: object) -> None:
        self._write(snapshot)

    def _write(self, fact: object) -> None:
        write = getattr(self._transport, "write", None)
        if (
            not callable(write)
            or write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": _json_value(fact),
                }
            )
            is not True
        ):
            disconnect = getattr(self._transport, "disconnect", None)
            if callable(disconnect):
                disconnect()


@dataclass
class _ObserverTransportState:
    in_flight: int = 0
    closed: bool = False
    reservations_released: bool = False
    active: dict[str, Registration] = field(default_factory=dict)


class _ObserverWireController:
    """Own transport-bound prepared and active Host Observer registrations."""

    def __init__(
        self,
        adapter: _ObserverAdapter,
        catalog: SessionCatalogV1Controller | None,
    ) -> None:
        self._adapter = adapter
        self._catalog = catalog
        self._lock = threading.RLock()
        self._transport_states: dict[object, _ObserverTransportState] = {}
        self._reserved_subscriptions = 0
        self._closed = False

    def dispatch(
        self,
        request: dict[str, Any],
        transport: object,
    ) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            raise TypeError("observer request must be an object")
        method = request.get("method")
        if method == "session.observe.subscribe":
            self._subscribe(request, transport)
            return None
        if method == "session.observe.unsubscribe":
            self._unsubscribe(request, transport)
            return None
        if method in SESSION_CATALOG_METHODS and self._catalog is not None:
            self._catalog.dispatch(request, transport)
            return None
        raise ValueError("observer method is unavailable")

    def _subscribe(self, request: dict[str, Any], transport: object) -> None:
        params = request.get("params")
        if not isinstance(params, Mapping):
            raise TypeError("observer params must be an object")
        write = getattr(transport, "write", None)
        if not callable(write):
            raise TypeError("observer transport is unavailable")
        state = self._begin_subscribe(transport)
        if state is None:
            return
        reservation_finalized = False
        try:
            prepared = self._adapter.prepare(
                dict(params),
                _ObserverWireSink(transport),
            )
            try:
                snapshot = _json_value(prepared.snapshot)
                if not isinstance(snapshot, dict):
                    raise TypeError("observer snapshot must be an object")
                subscription_id = str(uuid.uuid4())
                wrote = write(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {**snapshot, "subscription_id": subscription_id},
                    }
                )
                if wrote is not True:
                    prepared.close()
                    self._disconnect(transport)
                    return
                registration = _registration(prepared.activate())
            except BaseException:
                prepared.close()
                self._disconnect(transport)
                raise
            retained = self._commit_subscribe(
                transport,
                state,
                subscription_id,
                registration,
            )
            reservation_finalized = True
            if not retained:
                registration.close()
        finally:
            if not reservation_finalized:
                self._abort_subscribe(transport, state)

    def _unsubscribe(self, request: dict[str, Any], transport: object) -> None:
        params = request.get("params")
        if not isinstance(params, Mapping):
            raise TypeError("observer params must be an object")
        subscription_id = required_text(
            params.get("subscription_id"),
            "subscription_id",
        )
        with self._lock:
            state = self._transport_states.get(transport)
            registration = (
                state.active.pop(subscription_id, None) if state is not None else None
            )
            if registration is not None:
                self._reserved_subscriptions -= 1
                self._remove_if_idle_locked(transport, state)
        if registration is None:
            return
        registration.close()

    def close_transport(self, transport: object) -> None:
        registrations = self._claim_transport_registrations(transport)
        first_error: BaseException | None = None
        if self._catalog is not None:
            try:
                self._catalog.close_transport(transport)
            except BaseException as error:  # noqa: BLE001
                first_error = error
        for registration in registrations:
            try:
                registration.close()
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def rollover(self) -> None:
        with self._lock:
            transports = tuple(self._transport_states)
        first_error: BaseException | None = None
        for transport in transports:
            for registration in self._claim_transport_registrations(transport):
                try:
                    registration.close()
                except BaseException as error:  # noqa: BLE001
                    first_error = first_error or error
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        with self._lock:
            self._closed = True
            transports = tuple(self._transport_states)
        first_error: BaseException | None = None
        for transport in transports:
            try:
                self.close_transport(transport)
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if self._catalog is not None:
            try:
                self._catalog.close()
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def _begin_subscribe(
        self,
        transport: object,
    ) -> _ObserverTransportState | None:
        with self._lock:
            if self._closed:
                return None
            state = self._transport_states.get(transport)
            if state is None:
                state = _ObserverTransportState()
            if state.closed:
                return None
            if (
                state.in_flight + len(state.active)
                >= MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT
                or self._reserved_subscriptions >= MAX_OBSERVER_SUBSCRIPTIONS_TOTAL
            ):
                raise RuntimeError("observer subscription capacity exceeded")
            self._transport_states[transport] = state
            state.in_flight += 1
            self._reserved_subscriptions += 1
            return state

    def _commit_subscribe(
        self,
        transport: object,
        state: _ObserverTransportState,
        subscription_id: str,
        registration: Registration,
    ) -> bool:
        with self._lock:
            state.in_flight -= 1
            if state.closed or self._closed:
                if not state.reservations_released:
                    self._reserved_subscriptions -= 1
                self._remove_if_idle_locked(transport, state)
                return False
            state.active[subscription_id] = registration
            return True

    def _abort_subscribe(
        self,
        transport: object,
        state: _ObserverTransportState,
    ) -> None:
        with self._lock:
            state.in_flight -= 1
            if not state.reservations_released:
                self._reserved_subscriptions -= 1
            self._remove_if_idle_locked(transport, state)

    def _claim_transport_registrations(
        self,
        transport: object,
    ) -> tuple[Registration, ...]:
        with self._lock:
            state = self._transport_states.get(transport)
            if state is None:
                return ()
            state.closed = True
            registrations = tuple(state.active.values())
            if not state.reservations_released:
                self._reserved_subscriptions -= state.in_flight + len(registrations)
                state.reservations_released = True
            state.active.clear()
            self._remove_if_idle_locked(transport, state)
            return registrations

    def _remove_if_idle_locked(
        self,
        transport: object,
        state: _ObserverTransportState,
    ) -> None:
        if state.in_flight == 0 and not state.active:
            current = self._transport_states.get(transport)
            if current is state:
                self._transport_states.pop(transport, None)

    @staticmethod
    def _disconnect(transport: object) -> None:
        disconnect = getattr(transport, "disconnect", None)
        if callable(disconnect):
            disconnect()


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        try:
            return value[name]
        except KeyError as error:
            raise ValueError(f"runtime descriptor is missing {name}") from error
    try:
        return getattr(value, name)
    except AttributeError as error:
        raise ValueError(f"runtime descriptor is missing {name}") from error


def _validate_session_catalog_capability(
    _capabilities: frozenset[str],
    capability_versions: Mapping[str, int],
    *,
    host_spi_available: bool,
) -> None:
    version = capability_versions.get(SESSION_CATALOG_CAPABILITY)
    if version not in {None, 0, 1}:
        raise RuntimeError("session catalog capability version is unsupported")
    if version == 1 and not host_spi_available:
        raise RuntimeError("session catalog Host SPI is unavailable")


class _RuntimeBinding:
    def __init__(
        self,
        descriptor: object,
        *,
        lock: threading.RLock,
        capability_validator: (
            Callable[[frozenset[str], Mapping[str, int]], None] | None
        ) = None,
    ) -> None:
        self._lock = lock
        self._capability_validator = capability_validator
        self._profile = ""
        self._runtime_generation = ""
        self._capabilities: frozenset[str] = frozenset()
        self._capability_versions: dict[str, int] = {}
        self._ready = False
        self.update(descriptor)

    @property
    def profile(self) -> str:
        with self._lock:
            return self._profile

    @property
    def runtime_generation(self) -> str:
        with self._lock:
            return self._runtime_generation

    @property
    def owner_action_methods(self) -> frozenset[str]:
        with self._lock:
            return OWNER_ACTION_METHODS & self._capabilities

    def supports(self, capability: str) -> bool:
        with self._lock:
            return self._ready and capability in self._capabilities

    def capability_version(self, capability: str) -> int | None:
        with self._lock:
            return self._capability_versions.get(capability)

    def supports_version(self, capability: str, version: int) -> bool:
        with self._lock:
            return (
                self._ready
                and capability in self._capabilities
                and self._capability_versions.get(capability) == version
            )

    def update(
        self,
        descriptor: object,
        *,
        before_publish: Callable[[], None] | None = None,
    ) -> bool:
        profile = required_text(_field(descriptor, "profile"), "profile")
        generation = required_text(
            _field(descriptor, "runtime_generation"),
            "runtime_generation",
        )
        state = required_text(_field(descriptor, "state"), "state")
        raw_capabilities = _field(descriptor, "capabilities")
        if isinstance(raw_capabilities, Mapping):
            capability_versions = dict(raw_capabilities)
            capabilities = frozenset(
                capability
                for capability, version in raw_capabilities.items()
                if isinstance(version, int)
                and not isinstance(version, bool)
                and version > 0
            )
            if any(
                not isinstance(version, int) or isinstance(version, bool) or version < 0
                for version in raw_capabilities.values()
            ):
                raise ValueError(
                    "runtime capability versions must be non-negative integers"
                )
        elif isinstance(raw_capabilities, str):
            raise TypeError("runtime capabilities must be a collection")
        else:
            try:
                capabilities = frozenset(raw_capabilities)
            except TypeError as error:
                raise TypeError("runtime capabilities must be a collection") from error
            capability_versions = {capability: 1 for capability in capabilities}
        if any(
            not isinstance(capability, str)
            or not capability
            or capability != capability.strip()
            for capability in (
                raw_capabilities
                if isinstance(raw_capabilities, Mapping)
                else capabilities
            )
        ):
            raise ValueError("runtime capabilities must be canonical text")
        if self._capability_validator is not None:
            self._capability_validator(capabilities, capability_versions)
        with self._lock:
            previous_identity = self._profile, self._runtime_generation
            previous_capabilities = self._capabilities
            previous_capability_versions = self._capability_versions
            was_ready = self._ready
            changed = (
                (
                    previous_identity != ("", "")
                    and previous_identity != (profile, generation)
                )
                or (was_ready != (state == "ready"))
                or (previous_capabilities != capabilities)
                or (previous_capability_versions != capability_versions)
            )
            if changed and before_publish is not None:
                before_publish()
            self._profile = profile
            self._runtime_generation = generation
            self._capabilities = capabilities
            self._capability_versions = capability_versions
            self._ready = state == "ready"
            return changed

    def snapshot(self) -> tuple[str, str, bool]:
        with self._lock:
            return self._profile, self._runtime_generation, self._ready

    def matches(self, *, profile: str, runtime_generation: str) -> bool:
        with self._lock:
            return (
                self._ready
                and profile == self._profile
                and runtime_generation == self._runtime_generation
            )

    def require(self, *, profile: object, runtime_generation: object) -> None:
        requested_profile = required_text(profile, "profile")
        requested_generation = required_text(
            runtime_generation,
            "runtime_generation",
        )
        with self._lock:
            if (
                not self._ready
                or requested_profile != self._profile
                or requested_generation != self._runtime_generation
            ):
                raise SessionBindingMismatch("session binding mismatch")

    def require_owner_action(self, method: str) -> None:
        with self._lock:
            if method not in self._capabilities:
                raise OwnerActionMethodUnavailable("owner action method is unavailable")

    def require_ready(self) -> None:
        with self._lock:
            if not self._ready:
                raise RuntimeError("host runtime is not ready")

    def audit_current(
        self,
        host: GatewayExtensionHostV1,
        factory: object,
        *,
        action: str,
        state: str,
    ) -> None:
        if not callable(factory):
            raise TypeError("safe audit event constructor is unavailable")
        with self._lock:
            event = factory(
                name="runtime.lifecycle",
                profile=self._profile,
                runtime_generation=self._runtime_generation,
                attributes={"action": action, "state": state},
            )
            host.audit(event)


class _TrackedObserverRegistration:
    def __init__(
        self,
        owner: _ObserverGenerationResources,
        registration: Registration,
        delivery_gate: _ObserverSinkGate,
    ) -> None:
        self._owner = owner
        self._registration = registration
        self._delivery_gate = delivery_gate
        self._closed = False
        self._closing = False
        self._revoked = False

    def close(self) -> None:
        self._owner.close_subscription(self)


class _TrackedPreparedObserver:
    def __init__(
        self,
        owner: _ObserverGenerationResources,
        prepared: PreparedObserver,
        delivery_gate: _ObserverSinkGate,
        *,
        profile: str,
        runtime_generation: str,
    ) -> None:
        self._owner = owner
        self._prepared = prepared
        self._delivery_gate = delivery_gate
        self._profile = profile
        self._runtime_generation = runtime_generation
        self._closed = False
        self._closing = False
        self._activated = False
        self._revoked = False
        self.snapshot = prepared.snapshot
        self.activation_deadline_monotonic = prepared.activation_deadline_monotonic

    def activate(self) -> Registration:
        return self._owner.activate(self)

    def close(self) -> None:
        self._owner.close_prepared(self)


class _ObserverSinkGate:
    """Linearizable delivery fence for one runtime generation."""

    def __init__(
        self,
        sink: object,
        binding: _RuntimeBinding,
        *,
        profile: str,
        runtime_generation: str,
    ) -> None:
        self._sink = sink
        self._binding = binding
        self._profile = profile
        self._runtime_generation = runtime_generation
        self._condition = threading.Condition(threading.RLock())
        self._revoked = False
        self._in_flight: dict[int, int] = {}

    def revoke(self) -> None:
        self._mark_revoked()
        self._drain()

    def _mark_revoked(self) -> None:
        with self._condition:
            self._revoked = True

    def _drain(self) -> None:
        current_thread = threading.get_ident()
        with self._condition:
            if current_thread in self._in_flight:
                return
            while self._in_flight:
                self._condition.wait()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._deliver(self._sink, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._sink, name)
        if not callable(attribute):
            return attribute

        def deliver(*args: Any, **kwargs: Any) -> Any:
            return self._deliver(getattr(self._sink, name), *args, **kwargs)

        return deliver

    def _deliver(self, target: object, *args: Any, **kwargs: Any) -> Any:
        thread_id = threading.get_ident()
        with self._condition:
            if self._revoked or not self._binding.matches(
                profile=self._profile,
                runtime_generation=self._runtime_generation,
            ):
                return None
            self._in_flight[thread_id] = self._in_flight.get(thread_id, 0) + 1
        try:
            return target(*args, **kwargs)
        finally:
            with self._condition:
                remaining = self._in_flight[thread_id] - 1
                if remaining:
                    self._in_flight[thread_id] = remaining
                else:
                    del self._in_flight[thread_id]
                self._condition.notify_all()


class _ObserverGenerationResources:
    """Own prepared and active observers for exactly one current generation."""

    def __init__(self, binding: _RuntimeBinding) -> None:
        self._binding = binding
        self._lock = threading.RLock()
        self._prepared: set[_TrackedPreparedObserver] = set()
        self._subscriptions: set[_TrackedObserverRegistration] = set()

    def track(
        self,
        prepared: PreparedObserver,
        delivery_gate: _ObserverSinkGate,
        *,
        profile: str,
        runtime_generation: str,
    ) -> PreparedObserver:
        tracked = _TrackedPreparedObserver(
            self,
            prepared,
            delivery_gate,
            profile=profile,
            runtime_generation=runtime_generation,
        )
        changed = False
        with self._lock:
            if self._binding.matches(
                profile=profile,
                runtime_generation=runtime_generation,
            ):
                self._prepared.add(tracked)
                return tracked
            tracked._revoked = True
            self._prepared.add(tracked)
            changed = True
        if changed:
            delivery_gate.revoke()
        self.close_prepared(tracked)
        raise RuntimeError("runtime generation changed during observer preparation")

    def activate(self, prepared: _TrackedPreparedObserver) -> Registration:
        with self._lock:
            if prepared._revoked:
                raise RuntimeError("runtime generation changed")
            if prepared._closed or prepared not in self._prepared:
                raise RuntimeError("prepared observer is closed")
            if not self._binding.matches(
                profile=prepared._profile,
                runtime_generation=prepared._runtime_generation,
            ):
                prepared._revoked = True
            else:
                registration = _registration(prepared._prepared.activate())
                tracked = _TrackedObserverRegistration(
                    self,
                    registration,
                    prepared._delivery_gate,
                )
                self._prepared.remove(prepared)
                self._subscriptions.add(tracked)
                prepared._activated = True
                return tracked
        self.close_prepared(prepared)
        raise RuntimeError("runtime generation changed")

    def close_prepared(self, prepared: _TrackedPreparedObserver) -> None:
        with self._lock:
            if prepared._closed or prepared._activated:
                return
            prepared._revoked = True
            delivery_gate = prepared._delivery_gate
        delivery_gate.revoke()
        with self._lock:
            if prepared._closed or prepared._activated or prepared._closing:
                return
            prepared._closing = True
            raw_prepared = prepared._prepared
        try:
            raw_prepared.close()
        except BaseException:
            with self._lock:
                prepared._closing = False
            raise
        with self._lock:
            prepared._closed = True
            prepared._closing = False
            self._prepared.discard(prepared)

    def close_subscription(
        self,
        subscription: _TrackedObserverRegistration,
    ) -> None:
        with self._lock:
            if subscription._closed:
                return
            subscription._revoked = True
            delivery_gate = subscription._delivery_gate
        delivery_gate.revoke()
        with self._lock:
            if subscription._closed or subscription._closing:
                return
            subscription._closing = True
            registration = subscription._registration
        try:
            registration.close()
        except BaseException:
            with self._lock:
                subscription._closing = False
            raise
        with self._lock:
            subscription._closed = True
            subscription._closing = False
            self._subscriptions.discard(subscription)

    def revoke_all(self) -> None:
        with self._lock:
            prepared = tuple(self._prepared)
            subscriptions = tuple(self._subscriptions)
            for item in prepared:
                item._revoked = True
            for item in subscriptions:
                item._revoked = True
        gates = tuple(item._delivery_gate for item in (*prepared, *subscriptions))
        for gate in gates:
            gate._mark_revoked()
        for gate in gates:
            gate._drain()
        self.retry_revoked()

    def retry_revoked(self) -> None:
        with self._lock:
            prepared = tuple(item for item in self._prepared if item._revoked)
            subscriptions = tuple(item for item in self._subscriptions if item._revoked)
        first_error: BaseException | None = None
        for item in prepared:
            try:
                self.close_prepared(item)
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        for item in subscriptions:
            try:
                self.close_subscription(item)
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    close = revoke_all


class _RuntimeGenerationResources:
    def __init__(
        self,
        leases: ControlLeaseManager,
        observers: _ObserverGenerationResources,
        catalog: SessionCatalogV1Controller | None,
        wire_controller: _ObserverWireController,
    ) -> None:
        self._leases = leases
        self._observers = observers
        self._catalog = catalog
        self._wire_controller = wire_controller

    def fence_catalog(self) -> tuple[object, ...]:
        if self._catalog is None:
            return ()
        return self._catalog.fence_rollover()

    def rollover(
        self,
        *,
        profile: str,
        runtime_generation: str,
        ready: bool,
        catalog_fence: tuple[object, ...],
    ) -> None:
        self._wire_controller.rollover()
        self._leases.bind_runtime(
            profile=profile,
            runtime_generation=runtime_generation,
            ready=ready,
        )
        if self._catalog is not None:
            self._catalog.complete_rollover(catalog_fence)
        self._observers.revoke_all()

    def retry_cleanup(self) -> None:
        self._observers.retry_revoked()

    def close(self) -> None:
        self._leases.deactivate_runtime()
        if self._catalog is not None:
            self._catalog.close()
        self._observers.revoke_all()


class _LocalEndpointLifecycle:
    """Open and roll over all role endpoints as one fail-closed authority set."""

    def __init__(
        self,
        host: GatewayExtensionHostV1,
        endpoints: tuple[object, ...],
    ) -> None:
        self._host = host
        self._endpoints = endpoints
        self._lock = threading.RLock()
        self._registrations: dict[int, Registration] = {}
        self._closed = False
        self._starting = False
        self._pending_rebuild: bool | None = None

    def _register(self, endpoint: object) -> Registration:
        if getattr(endpoint, "connection_role", None) == "observer":
            contract = getattr(endpoint, "observer_contract", None)
            with observer_endpoint_contract(contract):
                return _registration(self._host.register_local_endpoint(endpoint))
        return _registration(self._host.register_local_endpoint(endpoint))

    def _open_all(self) -> None:
        opened: list[tuple[int, Registration]] = []
        try:
            for endpoint in self._endpoints:
                key = id(endpoint)
                registration = self._register(endpoint)
                self._registrations[key] = registration
                opened.append((key, registration))
        except BaseException:
            for key, registration in reversed(opened):
                with suppress(BaseException):
                    registration.close()
                    self._registrations.pop(key, None)
            raise

    def _close_all(self, *, reverse: bool) -> None:
        endpoints = reversed(self._endpoints) if reverse else iter(self._endpoints)
        first_error: BaseException | None = None
        for endpoint in endpoints:
            key = id(endpoint)
            registration = self._registrations.get(key)
            if registration is None:
                continue
            try:
                registration.close()
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
            else:
                self._registrations.pop(key, None)
        if first_error is not None:
            raise first_error

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("local endpoint lifecycle is closed")
            if self._registrations or self._starting:
                return
            self._starting = True
        succeeded = False
        try:
            self._open_all()
            succeeded = True
        finally:
            with self._lock:
                self._starting = False
                pending = self._pending_rebuild
                self._pending_rebuild = None
        if succeeded and pending is not None:
            self.rebuild(ready=pending)

    def rebuild(self, *, ready: bool) -> None:
        with self._lock:
            if self._closed:
                return
            if self._starting:
                self._pending_rebuild = ready
                return
            self._close_all(reverse=False)
            if ready:
                self._open_all()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._close_all(reverse=True)
            self._closed = True


class _RuntimeListener:
    def __init__(
        self,
        binding: _RuntimeBinding,
        resources: _RuntimeGenerationResources,
        endpoint_lifecycle: _LocalEndpointLifecycle,
    ) -> None:
        self._binding = binding
        self._resources = resources
        self._endpoint_lifecycle = endpoint_lifecycle
        self._lock = threading.RLock()
        self._pending_reconciliation: (
            tuple[str, str, bool, tuple[object, ...]] | None
        ) = None

    def __call__(self, descriptor: object) -> None:
        with self._lock:
            catalog_fence: tuple[object, ...] | None = None

            def fence_before_publish() -> None:
                nonlocal catalog_fence
                catalog_fence = self._resources.fence_catalog()

            if self._binding.update(
                descriptor,
                before_publish=fence_before_publish,
            ):
                profile, runtime_generation, ready = self._binding.snapshot()
                if catalog_fence is None:
                    raise RuntimeError("runtime generation fence is unavailable")
                self._pending_reconciliation = (
                    profile,
                    runtime_generation,
                    ready,
                    catalog_fence,
                )
            pending = self._pending_reconciliation
            if pending is None:
                self._resources.retry_cleanup()
                return
            profile, runtime_generation, ready, catalog_fence = pending
            self._resources.rollover(
                profile=profile,
                runtime_generation=runtime_generation,
                ready=ready,
                catalog_fence=catalog_fence,
            )
            self._endpoint_lifecycle.rebuild(ready=ready)
            self._pending_reconciliation = None

    def on_runtime_descriptor(self, descriptor: object) -> None:
        self(descriptor)


class _ObserverAdapter:
    def __init__(
        self,
        host: GatewayExtensionHostV1,
        binding: _RuntimeBinding,
        resources: _ObserverGenerationResources,
        observer_v2_bundle: ObserverV2Bundle | None,
        host_spi_factories: HostSpiFactories,
    ) -> None:
        self._host = host
        self._binding = binding
        self._resources = resources
        self._observer_v2_bundle = observer_v2_bundle
        self._host_spi_factories = host_spi_factories

    def prepare(self, value: object, sink: object) -> PreparedObserver:
        scope = self._scope(value)
        delivery_gate = _ObserverSinkGate(
            sink,
            self._binding,
            profile=scope.profile,
            runtime_generation=scope.runtime_generation,
        )
        observer_contract = self._observer_contract(value)
        if observer_contract == 2:
            return self._prepare_v2(scope, value, delivery_gate)
        return self._resources.track(
            _prepared_observer(
                self._host.prepare_observer(
                    self._host_spi_factories.observer_request(
                        profile=scope.profile,
                        durable_session_key=scope.durable_session_key,
                        runtime_generation=scope.runtime_generation,
                    ),
                    delivery_gate,
                ),
            ),
            delivery_gate,
            profile=scope.profile,
            runtime_generation=scope.runtime_generation,
        )

    def _prepare_v2(
        self,
        scope: Any,
        value: object,
        delivery_gate: _ObserverSinkGate,
    ) -> PreparedObserver:
        if self._observer_v2_bundle is None or not self._binding.supports_version(
            OUTPUT_PARITY_CAPABILITY, 1
        ):
            raise RuntimeError("observer output parity v2 is unavailable")
        if not isinstance(value, Mapping) or set(value) != {
            "observer_contract",
            "profile",
            "runtime_generation",
            "session_key",
        }:
            raise ValueError("observer v2 request must use the exact schema")
        projection_sink = _ObserverV2Sink(
            delivery_gate,
            self._observer_v2_bundle,
        )
        raw_prepared = _prepared_observer(
            self._host.prepare_observer(
                self._host_spi_factories.observer_request(
                    profile=scope.profile,
                    durable_session_key=scope.durable_session_key,
                    runtime_generation=scope.runtime_generation,
                    observer_contract=2,
                    required_capabilities=frozenset({OUTPUT_PARITY_CAPABILITY}),
                ),
                projection_sink,
            )
        )
        try:
            projection_sink.install_snapshot(raw_prepared.snapshot)
            prepared = _ObserverV2Prepared(
                raw_prepared,
                projection_sink,
                projection_sink.snapshot,
            )
        except BaseException:
            projection_sink.close()
            raw_prepared.close()
            raise
        return self._resources.track(
            prepared,
            delivery_gate,
            profile=scope.profile,
            runtime_generation=scope.runtime_generation,
        )

    def snapshot(self, value: object) -> object:
        return self._host.control_snapshot(self._scope(value))

    def _scope(self, value: object) -> Any:
        if not isinstance(value, Mapping):
            raise TypeError("scope must be an object")
        profile = value.get("profile")
        generation = value.get("runtime_generation")
        self._binding.require(
            profile=profile,
            runtime_generation=generation,
        )
        return self._host_spi_factories.control_scope(
            profile=required_text(profile, "profile"),
            durable_session_key=required_text(
                value.get("session_key"),
                "session_key",
            ),
            runtime_generation=required_text(
                generation,
                "runtime_generation",
            ),
        )

    @staticmethod
    def _observer_contract(value: object) -> int:
        if not isinstance(value, Mapping):
            raise TypeError("scope must be an object")
        observer_contract = value.get("observer_contract", 1)
        if type(observer_contract) is not int or observer_contract not in {1, 2}:
            raise ValueError("observer_contract must be 1 or 2")
        return observer_contract


class _ObserverV2Prepared:
    def __init__(
        self,
        prepared: PreparedObserver,
        sink: _ObserverV2Sink,
        snapshot: object,
    ) -> None:
        self._prepared = prepared
        self._sink = sink
        self.snapshot = snapshot
        self.activation_deadline_monotonic = prepared.activation_deadline_monotonic

    def activate(self) -> Registration:
        self._sink.begin_activation()
        try:
            registration = _registration(self._prepared.activate())
        except BaseException:
            self._sink.close()
            raise
        return _ObserverV2Registration(registration, self._sink)

    def close(self) -> None:
        self._sink.close()
        self._prepared.close()


class _ObserverV2Registration:
    def __init__(self, registration: Registration, sink: _ObserverV2Sink) -> None:
        self._registration = registration
        self._sink = sink

    def close(self) -> None:
        self._sink.close()
        self._registration.close()


class _ObserverV2Sink:
    def __init__(
        self,
        delivery_gate: _ObserverSinkGate,
        bundle: ObserverV2Bundle,
    ) -> None:
        self._delivery_gate = delivery_gate
        self._projection = ObserverV2Projection(bundle)
        self._lock = threading.RLock()
        self._phase = "preparing"
        self.snapshot: object | None = None

    def __call__(self, event: object) -> None:
        self.on_event(event)

    def install_snapshot(self, snapshot: object) -> None:
        with self._lock:
            if self._phase != "preparing":
                raise ObserverV2Violation("observer v2 preparation is unavailable")
            self.snapshot = self._projection.install_snapshot(
                _observer_v2_snapshot_fact(snapshot)
            )
            self._phase = "prepared"

    def begin_activation(self) -> None:
        with self._lock:
            if self._phase != "prepared":
                raise ObserverV2Violation("observer v2 preparation is unavailable")
            self._phase = "active"

    def close(self) -> None:
        with self._lock:
            if self._phase == "closed":
                return
            self._phase = "closed"
        self._delivery_gate.revoke()

    def on_snapshot(self, snapshot: object) -> None:
        with self._lock:
            if self._phase in {"closed", "failed"}:
                return
            self._require_active()
            try:
                normalized = self._projection.install_snapshot(
                    _observer_v2_snapshot_fact(snapshot)
                )
            except ObserverV2Violation:
                self._fail()
                raise
            self.snapshot = normalized
            self._delivery_gate.on_snapshot(normalized)

    def on_event(self, event: object) -> None:
        with self._lock:
            if self._phase in {"closed", "failed"}:
                return
            self._require_active()
            try:
                normalized = self._projection.accept_event(
                    _observer_v2_event_fact(event)
                )
            except ObserverV2Violation:
                self._fail()
                raise
            self._delivery_gate.on_event(normalized)

    def _require_active(self) -> None:
        if self._phase != "active":
            self._fail()
            raise ObserverV2Violation("observer v2 subscription is not active")

    def _fail(self) -> None:
        self._phase = "failed"
        self._delivery_gate.revoke()


def _observer_v2_snapshot_fact(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    normalized.setdefault("observer_contract", 2)
    for collection in _OBSERVER_V2_COLLECTIONS:
        normalized.setdefault(collection, [])
    return normalized


def _observer_v2_event_fact(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    normalized.setdefault("observer_contract", 2)
    return normalized


class _HostOwnerActionAdapter:
    def __init__(
        self,
        host: GatewayExtensionHostV1,
        binding: _RuntimeBinding,
        host_spi_factories: HostSpiFactories,
    ) -> None:
        self._host = host
        self._binding = binding
        self._host_spi_factories = host_spi_factories

    def validate(self, request: dict[str, Any], transport: object) -> None:
        self._prepare(request, transport)

    def __call__(
        self,
        request: dict[str, Any],
        transport: object,
    ) -> Mapping[str, Any]:
        params = request.get("params")
        if not isinstance(params, Mapping):
            raise TypeError("params must be an object")
        external_client_request_id = required_text(
            params.get("client_request_id"),
            "client_request_id",
        )
        host_request = self._prepare(request, transport)
        raw_result = self._host.invoke_owner_action(host_request)
        status, result_payload = self._result(raw_result)
        external_status = "unknown" if status == "effect_unknown" else status
        payload = {
            key: value
            for key, value in result_payload.items()
            if key not in {"client_request_id", "status"}
        }
        return MappingProxyType(
            {
                "status": external_status,
                "client_request_id": external_client_request_id,
                **payload,
            }
        )

    def _prepare(
        self,
        request: dict[str, Any],
        transport: object,
    ) -> object:
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        method = required_text(request.get("method"), "method")
        if method not in OWNER_ACTION_METHODS:
            raise ValueError("owner action method is unavailable")
        params = request.get("params")
        if not isinstance(params, dict):
            raise TypeError("params must be an object")
        claims = getattr(transport, "auth_claims", None)
        if not isinstance(claims, Mapping):
            raise TypeError("control claims are unavailable")
        profile = required_text(claims.get("profile"), "profile")
        durable_session_key = required_text(
            claims.get("session_key"),
            "session_key",
        )
        if params.get("profile", profile) != profile or (
            params.get("session_key", durable_session_key) != durable_session_key
        ):
            raise SessionBindingMismatch("session binding mismatch")
        runtime_generation = required_text(
            params.get("runtime_generation"),
            "runtime_generation",
        )
        self._binding.require(
            profile=profile,
            runtime_generation=runtime_generation,
        )
        self._binding.require_owner_action(method)
        required_text(
            params.get("client_request_id"),
            "client_request_id",
        )
        return self._host_spi_factories.owner_action_request(
            profile=profile,
            durable_session_key=durable_session_key,
            runtime_generation=runtime_generation,
            command_id=required_text(request.get("id"), "command_id"),
            method=method,
            payload={
                key: value
                for key, value in params.items()
                if key not in _OWNER_SCOPE_FIELDS
            },
        )

    @staticmethod
    def _result(value: object) -> tuple[str, Mapping[str, Any]]:
        if isinstance(value, Mapping):
            status = value.get("status")
            payload = value.get("payload", {})
        else:
            status = getattr(value, "status", None)
            payload = getattr(value, "payload", {})
        if status not in OWNER_ACTION_STATUSES:
            raise ValueError("invalid owner action status")
        return status, frozen_json_mapping(payload, "payload")


@dataclass(frozen=True)
class LocalGatewayEndpointDescriptor:
    _binding: _RuntimeBinding = field(repr=False)
    _endpoint_opener: Callable[[object, object], Registration] = field(repr=False)
    connection_role: Literal["local-gateway"] = field(
        default="local-gateway",
        init=False,
    )
    contract_version: Literal[1] = field(default=1, init=False)

    @property
    def available_capabilities(self) -> frozenset[str]:
        capabilities = {"session.observe"}
        if self._binding.supports("session.control"):
            capabilities.add("session.control")
        if self._binding.supports_version(OUTPUT_PARITY_CAPABILITY, 1):
            capabilities.add(OUTPUT_PARITY_CAPABILITY)
        if self._binding.supports_version(SESSION_CATALOG_CAPABILITY, 1):
            capabilities.add(SESSION_CATALOG_CAPABILITY)
        return frozenset(capabilities)

    def handle_local_hello(self, raw: object) -> str:
        return LocalContractV1Adapter(
            runtime_generation=self._binding.runtime_generation,
            available_capabilities=self.available_capabilities,
        ).handle_hello(raw)

    def open_local_endpoint(self, runtime: object) -> Registration:
        return _registration(self._endpoint_opener(self, runtime))


@dataclass(frozen=True)
class ObserverEndpointDescriptor:
    _binding: _RuntimeBinding = field(repr=False)
    _adapter: _ObserverAdapter = field(repr=False)
    _endpoint_opener: Callable[[object, object], Registration] = field(repr=False)
    _wire_controller: _ObserverWireController = field(repr=False)
    connection_role: Literal["observer"] = field(
        default="observer",
        init=False,
    )
    contract_version: Literal[1] = field(default=1, init=False)
    _observer_v2_available: bool = field(default=False, repr=False)

    @property
    def profile(self) -> str:
        return self._binding.profile

    @property
    def runtime_generation(self) -> str:
        return self._binding.runtime_generation

    @property
    def contract_versions(self) -> frozenset[int]:
        if self._observer_v2_available and self._binding.supports_version(
            OUTPUT_PARITY_CAPABILITY,
            1,
        ):
            return frozenset({1, 2})
        return frozenset({1})

    @property
    def observer_contract(self) -> Literal[1, 2]:
        return 2 if 2 in self.contract_versions else 1

    @property
    def available_methods(self) -> frozenset[str]:
        methods = set(_OBSERVER_METHODS)
        if self._binding.supports_version(SESSION_CATALOG_CAPABILITY, 1):
            methods.update(SESSION_CATALOG_METHODS)
        return frozenset(methods)

    @property
    def available_capabilities(self) -> frozenset[str]:
        capabilities = {"session.observe"}
        if 2 in self.contract_versions:
            capabilities.add(OUTPUT_PARITY_CAPABILITY)
        if self._binding.supports_version(SESSION_CATALOG_CAPABILITY, 1):
            capabilities.add(SESSION_CATALOG_CAPABILITY)
        return frozenset(capabilities)

    def prepare_observer(self, request: object, sink: object) -> PreparedObserver:
        return self._adapter.prepare(request, sink)

    def open_local_endpoint(self, runtime: object) -> Registration:
        return _registration(self._endpoint_opener(self, runtime))

    def handle_observer_request(
        self,
        request: dict[str, Any],
        transport: object,
    ) -> dict[str, Any] | None:
        return self._wire_controller.dispatch(request, transport)

    def transport_disconnected(self, transport: object) -> None:
        self._wire_controller.close_transport(transport)


@dataclass(frozen=True)
class ControlEndpointDescriptor:
    _binding: _RuntimeBinding = field(repr=False)
    _adapter: _ObserverAdapter = field(repr=False)
    _dispatcher: ControlRequestDispatcher = field(repr=False)
    _endpoint_opener: Callable[[object, object], Registration] = field(repr=False)
    connection_role: Literal["control"] = field(
        default="control",
        init=False,
    )
    contract_version: Literal[1] = field(default=1, init=False)

    @property
    def profile(self) -> str:
        return self._binding.profile

    @property
    def runtime_generation(self) -> str:
        return self._binding.runtime_generation

    @property
    def available_methods(self) -> frozenset[str]:
        return _CONTROL_INFRA_METHODS | self._binding.owner_action_methods

    def handle_control_request(
        self,
        request: dict[str, Any],
        transport: object,
    ) -> dict[str, Any]:
        return self._dispatcher.dispatch(request, transport)

    def transport_disconnected(self, transport: object) -> None:
        self._dispatcher.transport_disconnected(transport)

    def read_control_snapshot(self, scope: object) -> object:
        return self._adapter.snapshot(scope)

    def open_local_endpoint(self, runtime: object) -> Registration:
        return _registration(self._endpoint_opener(self, runtime))


def _close_created(registrations: list[Registration]) -> None:
    for registration in reversed(registrations):
        with suppress(BaseException):
            registration.close()


def _registration(value: object) -> Registration:
    if not callable(getattr(value, "close", None)):
        raise TypeError("Host SPI v1 method must return a Registration")
    return value


def _prepared_observer(value: object) -> PreparedObserver:
    if not hasattr(value, "snapshot"):
        raise TypeError("Host SPI v1 prepare_observer must expose a snapshot")
    deadline = getattr(value, "activation_deadline_monotonic", None)
    if (
        not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or deadline <= 0
    ):
        raise TypeError(
            "Host SPI v1 prepare_observer must expose an activation deadline"
        )
    if not callable(getattr(value, "activate", None)) or not callable(
        getattr(value, "close", None)
    ):
        raise TypeError("Host SPI v1 prepare_observer must return a PreparedObserver")
    return value


class HermesAgentPluginExtension:
    """Install observer and explicit control through Host SPI v1 only."""

    def __init__(
        self,
        *,
        host_spi_factories: HostSpiFactories | None = None,
        endpoint_opener: Callable[[object, object], Registration] | None = None,
    ) -> None:
        self._host_spi_factories = (
            load_public_host_spi_factories()
            if host_spi_factories is None
            else host_spi_factories
        )
        self._endpoint_opener = endpoint_opener or _unavailable_endpoint_opener

    def install(self, host: GatewayExtensionHostV1) -> Registration:
        host_api_version = getattr(host, "host_api_version", None)
        if type(host_api_version) is not int or host_api_version != HOST_API_VERSION:
            raise RuntimeError("Hermes Gateway Extension Host API v1 is required")
        descriptor = host.runtime_descriptor()
        generation_lock = threading.RLock()
        catalog_request_factory = self._host_spi_factories.session_catalog_request
        catalog_host_spi_available = (
            catalog_request_factory is not None
            and callable(getattr(host, "session_catalog", None))
            and callable(getattr(host, "add_session_catalog_listener", None))
        )
        binding = _RuntimeBinding(
            descriptor,
            lock=generation_lock,
            capability_validator=lambda capabilities, versions: (
                _validate_session_catalog_capability(
                    capabilities,
                    versions,
                    host_spi_available=catalog_host_spi_available,
                )
            ),
        )
        binding.require_ready()
        observer_v2_bundle = None
        output_parity_version = binding.capability_version(OUTPUT_PARITY_CAPABILITY)
        if output_parity_version not in {None, 0, 1}:
            raise RuntimeError(
                "observer output parity capability version is unsupported"
            )
        if output_parity_version == 1:
            try:
                observer_v2_bundle = load_observer_v2_bundle()
            except ObserverV2Violation as error:
                raise RuntimeError(
                    "observer output parity v2 contract is unavailable"
                ) from error
        profile = binding.profile
        generation = binding.runtime_generation
        leases = ControlLeaseManager()
        observer_resources = _ObserverGenerationResources(binding)
        catalog_controller = (
            SessionCatalogV1Controller(
                host=host,
                binding=binding,
                request_factory=catalog_request_factory,
                lock=generation_lock,
            )
            if catalog_request_factory is not None and catalog_host_spi_available
            else None
        )
        leases.bind_runtime(
            profile=profile,
            runtime_generation=generation,
            ready=True,
        )
        observer_adapter = _ObserverAdapter(
            host,
            binding,
            observer_resources,
            observer_v2_bundle,
            self._host_spi_factories,
        )
        owner_adapter = _HostOwnerActionAdapter(
            host,
            binding,
            self._host_spi_factories,
        )
        dispatcher = ControlRequestDispatcher(
            owner_action=owner_adapter,
            owner_action_validator=owner_adapter.validate,
            binding_validator=lambda control_binding: binding.require(
                profile=control_binding.profile,
                runtime_generation=control_binding.runtime_generation,
            ),
            leases=leases,
        )
        wire_controller = _ObserverWireController(
            observer_adapter,
            catalog_controller,
        )
        generation_resources = _RuntimeGenerationResources(
            leases,
            observer_resources,
            catalog_controller,
            wire_controller,
        )
        local_gateway_endpoint = LocalGatewayEndpointDescriptor(
            _binding=binding,
            _endpoint_opener=self._endpoint_opener,
        )
        observer_endpoint = ObserverEndpointDescriptor(
            _binding=binding,
            _adapter=observer_adapter,
            _endpoint_opener=self._endpoint_opener,
            _wire_controller=wire_controller,
            _observer_v2_available=observer_v2_bundle is not None,
        )
        control_endpoint = ControlEndpointDescriptor(
            _binding=binding,
            _adapter=observer_adapter,
            _dispatcher=dispatcher,
            _endpoint_opener=self._endpoint_opener,
        )
        endpoint_lifecycle = _LocalEndpointLifecycle(
            host,
            (local_gateway_endpoint, observer_endpoint, control_endpoint),
        )
        created: list[Registration] = [generation_resources, wire_controller]
        try:
            created.append(
                _registration(
                    host.add_runtime_listener(
                        _RuntimeListener(
                            binding,
                            generation_resources,
                            endpoint_lifecycle,
                        )
                    )
                )
            )
            created.append(endpoint_lifecycle)
            endpoint_lifecycle.start()
            binding.audit_current(
                host,
                self._host_spi_factories.safe_audit_event,
                action="started",
                state="ready",
            )
        except BaseException:
            _close_created(created)
            with suppress(BaseException):
                binding.audit_current(
                    host,
                    self._host_spi_factories.safe_audit_event,
                    action="failed",
                    state="unavailable",
                )
            raise

        def audit_closed() -> None:
            binding.audit_current(
                host,
                self._host_spi_factories.safe_audit_event,
                action="closed",
                state="closed",
            )

        return CompositeRegistration(created, on_closed=audit_closed)


__all__ = [
    "MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT",
    "MAX_OBSERVER_SUBSCRIPTIONS_TOTAL",
    "ControlEndpointDescriptor",
    "HermesAgentPluginExtension",
    "LocalGatewayEndpointDescriptor",
    "ObserverEndpointDescriptor",
]
