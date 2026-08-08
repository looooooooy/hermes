"""Windows Local Relay backend with validated Gateway, Control, and Observer roles."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ....ports.local_relay import current_observer_endpoint_contract
from ..capabilities import PlatformLocalGatewayUnavailable
from . import control_relay, observer_relay
from .local_gateway_transport import create_local_gateway_resource

_STARTUP_TIMEOUT_SECONDS = 3.0
_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_UNAVAILABLE = "windows_cross_process_relay_hub_not_implemented"


class _Registration:
    def __init__(self, close_callback: Callable[[], None]) -> None:
        self._close_callback = close_callback
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_callback()


class WindowsLocalRelayBackend:
    """Run implemented Windows relay roles and fail closed for unfinished hubs."""

    def start_local_gateway_endpoint(self, **kwargs: Any) -> _Registration:
        resource = create_local_gateway_resource(**kwargs)
        resource.start(time.monotonic() + _STARTUP_TIMEOUT_SECONDS)
        return _Registration(
            lambda: resource.stop(time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS)
        )

    def start_control_endpoint(self, **kwargs: Any) -> object:
        return control_relay.start_control_endpoint(**kwargs)

    def list_control_endpoints(self) -> list[object]:
        return list(control_relay.list_control_endpoints())

    def start_observer_endpoint(self, **kwargs: Any) -> object:
        kwargs.setdefault(
            "observer_contract",
            current_observer_endpoint_contract(),
        )
        return observer_relay.start_observer_endpoint(**kwargs)

    def list_observer_endpoints(self) -> list[object]:
        return list(observer_relay.list_observer_endpoints())

    @staticmethod
    def _unavailable() -> None:
        raise PlatformLocalGatewayUnavailable(_UNAVAILABLE)

    def create_control_relay_hub(self, *, current_pid: int | None) -> None:
        del current_pid
        self._unavailable()

    def create_observer_relay_hub(self, *, current_pid: int | None) -> None:
        del current_pid
        self._unavailable()


LOCAL_RELAY_BACKEND = WindowsLocalRelayBackend()


def create_local_relay_backend() -> WindowsLocalRelayBackend:
    return LOCAL_RELAY_BACKEND


__all__ = [
    "LOCAL_RELAY_BACKEND",
    "WindowsLocalRelayBackend",
    "create_local_relay_backend",
]
