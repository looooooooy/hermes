"""Platform relay backend port configured by the Plugin composition root."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol


class OwnerActionDispatcherPort(Protocol):
    """Process-bounded execution owned by the Host runtime lifecycle."""

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any] | None: ...

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None: ...


class LocalRelayBackendPort(Protocol):
    """Platform operations behind the canonical local relay API."""

    def start_local_gateway_endpoint(self, **kwargs: Any) -> Any: ...

    def start_control_endpoint(self, **kwargs: Any) -> Any: ...

    def list_control_endpoints(self) -> list[Any]: ...

    def create_control_relay_hub(self, *, current_pid: int | None) -> Any: ...

    def start_observer_endpoint(self, **kwargs: Any) -> Any: ...

    def list_observer_endpoints(self) -> list[Any]: ...

    def create_observer_relay_hub(self, *, current_pid: int | None) -> Any: ...


class LocalRelayBackendNotConfigured(RuntimeError):
    """Raised when a relay API is used outside the Plugin composition root."""


_backend_factory: Callable[[], LocalRelayBackendPort] | None = None
_observer_endpoint_contract: ContextVar[int] = ContextVar(
    "observer_endpoint_contract",
    default=1,
)


def current_observer_endpoint_contract() -> int:
    """Return the exact Observer wire contract for synchronous registration."""

    return _observer_endpoint_contract.get()


@contextmanager
def observer_endpoint_contract(value: int):
    """Scope one Host endpoint registration to its negotiated wire contract."""

    if type(value) is not int or value not in {1, 2}:
        raise ValueError("observer_contract must be 1 or 2")
    token = _observer_endpoint_contract.set(value)
    try:
        yield
    finally:
        _observer_endpoint_contract.reset(token)


def configure_local_relay_backend(
    factory: Callable[[], LocalRelayBackendPort],
) -> None:
    """Install the current platform backend factory without starting I/O."""

    global _backend_factory
    _backend_factory = factory


def get_local_relay_backend() -> LocalRelayBackendPort:
    """Return the configured backend, failing closed if composition is absent."""

    if _backend_factory is None:
        raise LocalRelayBackendNotConfigured("local_relay_backend_not_configured")
    return _backend_factory()


__all__ = [
    "LocalRelayBackendNotConfigured",
    "LocalRelayBackendPort",
    "OwnerActionDispatcherPort",
    "configure_local_relay_backend",
    "current_observer_endpoint_contract",
    "get_local_relay_backend",
    "observer_endpoint_contract",
]
