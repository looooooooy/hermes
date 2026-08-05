"""Platform-neutral explicit-control relay contract and facade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ...ports.local_relay import (
    LocalRelayBackendPort,
    OwnerActionDispatcherPort,
    get_local_relay_backend,
)
from .control_v1 import (
    CONTROL_AVAILABLE_METHODS,
    CONTROL_ERROR_CODES,
    is_canonical_client_instance_id,
)

_ALLOWED_METHODS = CONTROL_AVAILABLE_METHODS
_CLAIM_KEYS = (
    "user_id",
    "provider",
    "connection_role",
    "client_instance_id",
    "session_key",
    "profile",
)
_REQUIRED_CLAIMS = _CLAIM_KEYS


@dataclass(frozen=True)
class ControlEndpoint:
    """Non-secret discovery metadata shared by every platform backend."""

    pid: int
    profile: str
    socket_path: Any
    instance_id: str

    def __post_init__(self) -> None:
        if not is_canonical_client_instance_id(self.instance_id):
            raise ValueError("instance_id must be a canonical RFC 4122 UUID")


class ControlEndpointRegistration(Protocol):
    """Closable platform registration returned by ``start_control_endpoint``."""

    def close(self) -> None: ...


def _sanitize_claims(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    claims = {
        key: item
        for key in _CLAIM_KEYS
        if isinstance((item := value.get(key)), str)
        and bool(item.strip())
        and item == item.strip()
    }
    if any(key not in claims for key in _REQUIRED_CLAIMS):
        return None
    if claims["connection_role"] != "control":
        return None
    if not is_canonical_client_instance_id(claims["client_instance_id"]):
        return None
    return claims


def _rpc_error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _relay_overloaded(request_id: Any) -> dict:
    return _rpc_error(
        request_id,
        CONTROL_ERROR_CODES["relay_overloaded"],
        "relay_overloaded",
    )


def _resolve_backend(
    backend: LocalRelayBackendPort | None,
) -> LocalRelayBackendPort:
    return backend if backend is not None else get_local_relay_backend()


def start_control_endpoint(
    *,
    authority: Any,
    dispatcher: Callable[[dict, Any], dict | None],
    transport_cleanup: Callable[[Any], None] | None = None,
    backend: LocalRelayBackendPort | None = None,
    owner_action_dispatcher: OwnerActionDispatcherPort | None = None,
) -> ControlEndpointRegistration:
    """Start the configured platform endpoint without owning platform I/O."""

    if transport_cleanup is None:
        candidate = getattr(dispatcher, "transport_disconnected", None)
        if callable(candidate):
            transport_cleanup = candidate
    endpoint_arguments = {
        "authority": authority,
        "dispatcher": dispatcher,
        "transport_cleanup": transport_cleanup,
    }
    if owner_action_dispatcher is not None:
        endpoint_arguments["owner_action_dispatcher"] = owner_action_dispatcher
    return _resolve_backend(backend).start_control_endpoint(
        **endpoint_arguments,
    )


def list_control_endpoints(
    *,
    backend: LocalRelayBackendPort | None = None,
) -> list[ControlEndpoint]:
    """Read trusted discovery metadata through the platform backend."""

    return _resolve_backend(backend).list_control_endpoints()


class ControlRelayHub:
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
            self._delegate = _resolve_backend(self._backend).create_control_relay_hub(
                current_pid=self._current_pid
            )
        return self._delegate

    def call(
        self,
        request: dict,
        *,
        transport: Any,
        auth_claims: Mapping[str, Any],
        profile: str | None,
    ) -> dict | None:
        return self._hub().call(
            request,
            transport=transport,
            auth_claims=auth_claims,
            profile=profile,
        )

    def close_transport(self, transport: Any) -> int:
        return self._hub().close_transport(transport)


__all__ = [
    "ControlEndpoint",
    "ControlEndpointRegistration",
    "ControlRelayHub",
    "list_control_endpoints",
    "start_control_endpoint",
]
