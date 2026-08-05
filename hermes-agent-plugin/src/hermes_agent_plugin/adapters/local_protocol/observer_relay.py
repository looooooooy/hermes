"""Platform-neutral read-only observer relay contract and facade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...ports.local_relay import (
    LocalRelayBackendPort,
    get_local_relay_backend,
)
from .control_v1 import is_canonical_client_instance_id


@dataclass(frozen=True)
class ObserverEndpoint:
    """Non-secret discovery metadata shared by every platform backend."""

    pid: int
    profile: str
    runtime_generation: str
    socket_path: Any
    instance_id: str

    def __post_init__(self) -> None:
        if not is_canonical_client_instance_id(self.instance_id):
            raise ValueError("instance_id must be a canonical RFC 4122 UUID")


class ObserverRelayRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ObserverRelayOwnershipError(RuntimeError):
    pass


class ObserverEndpointRegistration(Protocol):
    """Closable platform registration returned by ``start_observer_endpoint``."""

    def close(self) -> None: ...


def _resolve_backend(
    backend: LocalRelayBackendPort | None,
) -> LocalRelayBackendPort:
    return backend if backend is not None else get_local_relay_backend()


def start_observer_endpoint(
    *,
    authority: Any,
    dispatch: Callable[[dict[str, Any], Any], dict[str, Any] | None],
    remove_observer_subscriptions: Callable[[Any], None],
    backend: LocalRelayBackendPort | None = None,
) -> ObserverEndpointRegistration:
    """Start the configured platform endpoint without owning platform I/O."""

    return _resolve_backend(backend).start_observer_endpoint(
        authority=authority,
        dispatch=dispatch,
        remove_observer_subscriptions=remove_observer_subscriptions,
    )


def list_observer_endpoints(
    *,
    backend: LocalRelayBackendPort | None = None,
) -> list[ObserverEndpoint]:
    """Read trusted discovery metadata through the platform backend."""

    return _resolve_backend(backend).list_observer_endpoints()


class ObserverRelayHub:
    """Stable canonical hub that delegates transport work to a backend."""

    def __init__(
        self,
        *,
        current_pid: int | None = None,
        backend: LocalRelayBackendPort | None = None,
    ) -> None:
        self._current_pid = current_pid
        self._backend = backend
        self._delegate: Any | None = None

    def _hub(self) -> Any:
        if self._delegate is None:
            self._delegate = _resolve_backend(self._backend).create_observer_relay_hub(
                current_pid=self._current_pid
            )
        return self._delegate

    def subscribe(
        self,
        session_key: str,
        profile: str,
        transport: Any,
        *,
        runtime_generation: str,
    ) -> dict | None:
        return self._hub().subscribe(
            session_key,
            profile,
            transport,
            runtime_generation=runtime_generation,
        )

    def activate(self, subscription_id: str, transport: Any) -> bool | None:
        return self._hub().activate(subscription_id, transport)

    def unsubscribe(self, subscription_id: str, transport: Any) -> bool | None:
        return self._hub().unsubscribe(subscription_id, transport)

    def close_transport(self, transport: Any) -> int:
        return self._hub().close_transport(transport)


observer_relay_hub = ObserverRelayHub()

__all__ = [
    "ObserverEndpoint",
    "ObserverEndpointRegistration",
    "ObserverRelayHub",
    "ObserverRelayOwnershipError",
    "ObserverRelayRpcError",
    "list_observer_endpoints",
    "observer_relay_hub",
    "start_observer_endpoint",
]
