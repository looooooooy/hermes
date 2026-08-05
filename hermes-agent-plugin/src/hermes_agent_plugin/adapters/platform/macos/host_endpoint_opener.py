"""Open Host SPI endpoints with one process-scoped macOS authority."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from ....ports.local_relay import LocalRelayBackendPort
from .runtime_descriptor_v2 import (
    MacOSHostAuthorityV2,
    capture_macos_host_authority,
)


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _runtime_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError(f"runtime descriptor {name} is invalid")
    return value


class MacOSHostEndpointOpener:
    """Bind Observer and Control to one immutable Host process identity."""

    def __init__(
        self,
        *,
        backend: LocalRelayBackendPort,
        host_authority_factory: Callable[..., MacOSHostAuthorityV2] = (
            capture_macos_host_authority
        ),
    ) -> None:
        self._backend = backend
        self._host_authority_factory = host_authority_factory
        self._lock = threading.RLock()
        self._host_authority: MacOSHostAuthorityV2 | None = None
        self._runtime_generation: str | None = None
        self._runtime_authority: object | None = None

    def __call__(self, endpoint: object, runtime: object) -> object:
        profile = _runtime_text(_field(runtime, "profile"), "profile")
        runtime_generation = _runtime_text(
            _field(runtime, "runtime_generation"),
            "runtime_generation",
        )
        host_bundle_id = _runtime_text(
            _field(runtime, "host_bundle_id"),
            "host_bundle_id",
        )
        if _field(runtime, "state") != "ready":
            raise RuntimeError("host runtime is unavailable")
        with self._lock:
            authority = self._host_authority
            if authority is None:
                authority = self._host_authority_factory(
                    profile=profile,
                    host_bundle_id=host_bundle_id,
                )
                self._host_authority = authority
            elif (
                authority.profile != profile
                or authority.host_bundle_id != host_bundle_id
            ):
                raise RuntimeError("host runtime identity changed")
            if self._runtime_generation != runtime_generation:
                self._runtime_authority = authority.bind_runtime(runtime_generation)
                self._runtime_generation = runtime_generation
            runtime_authority = self._runtime_authority
            if runtime_authority is None:
                raise RuntimeError("host runtime authority is unavailable")

        role = getattr(endpoint, "connection_role", None)
        if role == "local-gateway":
            hello_handler = getattr(endpoint, "handle_local_hello", None)
            if not callable(hello_handler):
                raise TypeError("local gateway endpoint is incomplete")
            return self._backend.start_local_gateway_endpoint(
                authority=runtime_authority,
                hello_handler=hello_handler,
            )
        if role == "observer":
            dispatch = getattr(endpoint, "handle_observer_request", None)
            disconnected = getattr(endpoint, "transport_disconnected", None)
            if not callable(dispatch) or not callable(disconnected):
                raise TypeError("observer endpoint is incomplete")
            return self._backend.start_observer_endpoint(
                authority=runtime_authority,
                dispatch=dispatch,
                remove_observer_subscriptions=disconnected,
            )
        if role == "control":
            dispatch = getattr(endpoint, "handle_control_request", None)
            disconnected = getattr(endpoint, "transport_disconnected", None)
            if not callable(dispatch) or not callable(disconnected):
                raise TypeError("control endpoint is incomplete")
            return self._backend.start_control_endpoint(
                authority=runtime_authority,
                dispatcher=dispatch,
                transport_cleanup=disconnected,
            )
        raise ValueError("local endpoint role is unavailable")


__all__ = ["MacOSHostEndpointOpener"]
