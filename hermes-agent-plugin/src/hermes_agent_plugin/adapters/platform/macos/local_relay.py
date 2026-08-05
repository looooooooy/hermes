"""macOS composition of the control and observer UDS relay backends."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ....ports.local_relay import current_observer_endpoint_contract
from . import control_relay, observer_relay
from .local_gateway_paths import (
    MacOSLocalGatewayPaths,
    load_local_gateway_paths,
    provision_distinct_local_gateway_directories,
)
from .local_gateway_transport import create_local_gateway_resource
from .runtime_descriptor_v2 import (
    current_process_identity,
    require_current_process_authority,
)

_LOCAL_GATEWAY_LIFECYCLE_TIMEOUT_S = 3.0
_log = logging.getLogger(__name__)


def _require_authority_before_paths(kwargs: dict[str, Any]) -> None:
    require_current_process_authority(
        kwargs.get("authority"),
        process_identity_provider=kwargs.get(
            "process_identity_provider",
            current_process_identity,
        ),
    )


class _LocalGatewayEndpointRegistration:
    def __init__(self, resource: Any) -> None:
        self._resource = resource
        self._lock = threading.RLock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._resource.stop(time.monotonic() + _LOCAL_GATEWAY_LIFECYCLE_TIMEOUT_S)
            self._closed = True


class MacOSLocalRelayBackend:
    """Own macOS endpoint discovery, UDS clients, and relay lifecycles."""

    def __init__(self, paths: MacOSLocalGatewayPaths) -> None:
        self._paths = paths

    def start_local_gateway_endpoint(self, **kwargs: Any) -> Any:
        _require_authority_before_paths(kwargs)
        with provision_distinct_local_gateway_directories(self._paths):
            resource = create_local_gateway_resource(paths=self._paths, **kwargs)
            deadline = time.monotonic() + _LOCAL_GATEWAY_LIFECYCLE_TIMEOUT_S
            try:
                resource.start(deadline)
            except BaseException:
                try:
                    resource.stop(time.monotonic() + _LOCAL_GATEWAY_LIFECYCLE_TIMEOUT_S)
                except BaseException:  # noqa: BLE001
                    _log.warning("local gateway start cleanup failed")
                raise
            return _LocalGatewayEndpointRegistration(resource)

    def start_control_endpoint(self, **kwargs: Any) -> Any:
        _require_authority_before_paths(kwargs)
        with provision_distinct_local_gateway_directories(self._paths):
            return control_relay.start_control_endpoint(paths=self._paths, **kwargs)

    def list_control_endpoints(self) -> list[Any]:
        return control_relay.list_control_endpoints(paths=self._paths)

    def create_control_relay_hub(self, *, current_pid: int | None) -> Any:
        return control_relay.ControlRelayHub(
            current_pid=current_pid,
            paths=self._paths,
        )

    def start_observer_endpoint(self, **kwargs: Any) -> Any:
        _require_authority_before_paths(kwargs)
        kwargs.setdefault(
            "observer_contract",
            current_observer_endpoint_contract(),
        )
        with provision_distinct_local_gateway_directories(self._paths):
            return observer_relay.start_observer_endpoint(paths=self._paths, **kwargs)

    def list_observer_endpoints(self) -> list[Any]:
        return observer_relay.list_observer_endpoints(paths=self._paths)

    def create_observer_relay_hub(self, *, current_pid: int | None) -> Any:
        return observer_relay.ObserverRelayHub(
            current_pid=current_pid,
            paths=self._paths,
        )


def create_local_relay_backend(
    paths: MacOSLocalGatewayPaths | None = None,
) -> MacOSLocalRelayBackend:
    return MacOSLocalRelayBackend(paths or load_local_gateway_paths())


__all__ = [
    "MacOSLocalRelayBackend",
    "create_local_relay_backend",
]
