"""Windows Local Relay backend with a verified Local Gateway foundation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..capabilities import PlatformLocalGatewayUnavailable
from .local_gateway_transport import create_local_gateway_resource

_STARTUP_TIMEOUT_SECONDS = 3.0
_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_UNAVAILABLE = "windows_observer_control_not_implemented"


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
    """Run the Windows Local Gateway role and fail closed for unfinished roles."""

    def start_local_gateway_endpoint(self, **kwargs: Any) -> _Registration:
        resource = create_local_gateway_resource(**kwargs)
        resource.start(time.monotonic() + _STARTUP_TIMEOUT_SECONDS)
        return _Registration(
            lambda: resource.stop(time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS)
        )

    @staticmethod
    def _unavailable() -> None:
        raise PlatformLocalGatewayUnavailable(_UNAVAILABLE)

    def start_control_endpoint(self, **_kwargs: object) -> None:
        self._unavailable()

    def list_control_endpoints(self) -> list[object]:
        self._unavailable()

    def create_control_relay_hub(self, *, current_pid: int | None) -> None:
        del current_pid
        self._unavailable()

    def start_observer_endpoint(self, **_kwargs: object) -> None:
        self._unavailable()

    def list_observer_endpoints(self) -> list[object]:
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
