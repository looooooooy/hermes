"""Production explicit-control request dispatcher."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..adapters.local_protocol.control_v1 import CONTROL_ERROR_CODES
from ..domain.control_lease import (
    ControlBinding,
    ControlLeaseError,
    ControlLeaseManager,
    ControllerConflict,
    LeaseExpired,
    LeaseMismatch,
    LeaseRequired,
    SessionBindingMismatch,
)
from .control_commands import (
    CommandIdentity,
    CommandLedger,
    CommandOwnershipMismatch,
    RequestPayloadConflict,
)

_MUTATION_METHODS = frozenset(
    {
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    }
)
_LEASE_METHODS = frozenset(
    {
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
    }
)


class _DispatchError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OwnerActionMethodUnavailable(RuntimeError):
    """The authoritative Host revoked or never exposed this owner action."""


class ControlRequestDispatcher:
    """Bind control leases and idempotency to authenticated UDS transports."""

    def __init__(
        self,
        *,
        owner_action: Callable[[dict[str, Any], Any], Mapping[str, Any]],
        owner_action_validator: (Callable[[dict[str, Any], Any], None] | None) = None,
        binding_validator: Callable[[ControlBinding], None] | None = None,
        leases: ControlLeaseManager | None = None,
        commands: CommandLedger | None = None,
        desktop_controller_present: Callable[[], bool] = lambda: False,
    ) -> None:
        self._owner_action = owner_action
        self._owner_action_validator = owner_action_validator
        self._binding_validator = binding_validator
        self._leases = leases or ControlLeaseManager()
        self._commands = commands or CommandLedger()
        self._desktop_controller_present = desktop_controller_present

    def __call__(self, request: dict, transport: Any) -> dict:
        return self.dispatch(request, transport)

    def dispatch(self, request: dict, transport: Any) -> dict:
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise _DispatchError(-32600, "invalid request")
            method = request.get("method")
            params = request.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise _DispatchError(-32602, "invalid params")
            if method == "session.control.acquire":
                result = self._leases.acquire(self._binding(transport, params)).result()
            elif method == "session.control.renew":
                result = self._leases.renew(
                    self._binding(transport, params),
                    lease_id=self._lease_id(params),
                ).result()
            elif method == "session.control.release":
                result = self._leases.release(
                    self._binding(transport, params),
                    lease_id=self._lease_id(params),
                )
            elif method == "session.control.status":
                claims = self._claims(transport)
                self._require_target(params, claims)
                result = self._leases.status(
                    session_key=claims["session_key"],
                    profile=claims["profile"],
                    desktop_controller_present=bool(self._desktop_controller_present()),
                )
            elif method == "session.command.status":
                result = self._command_status(transport, params)
            elif method in _MUTATION_METHODS:
                result = self._mutate(
                    method=method,
                    request=request,
                    transport=transport,
                    params=params,
                )
            elif method in _LEASE_METHODS:
                raise _DispatchError(-32602, "invalid params")
            else:
                raise _DispatchError(
                    CONTROL_ERROR_CODES["method_not_allowed"],
                    "method_not_allowed",
                )
        except Exception as error:  # noqa: BLE001
            code, message = self._error(error)
            return self._rpc_error(request_id, code, message)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": dict(result),
        }

    def transport_disconnected(self, transport: Any) -> None:
        transport_id = getattr(transport, "transport_id", None)
        if isinstance(transport_id, str):
            self._leases.transport_disconnected(transport_id)

    def _mutate(
        self,
        *,
        method: str,
        request: dict[str, Any],
        transport: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        binding = self._binding(transport, params)
        self._leases.authorize(binding, lease_id=self._lease_id(params))
        if self._owner_action_validator is not None:
            self._owner_action_validator(request, transport)
        request_id = self._required_text(
            params.get("client_request_id"),
            "client_request_id",
        )
        identity = CommandIdentity(
            session_key=binding.session_key,
            user_id=binding.user_id,
            provider=binding.provider,
            client_instance_id=binding.client_instance_id,
        )
        canonical_payload = {
            key: value
            for key, value in params.items()
            if key not in {"lease_id", "relay_local_only"}
        }
        result = self._commands.execute(
            identity,
            method=method,
            client_request_id=request_id,
            payload=canonical_payload,
            operation=lambda: self._owner_action(request, transport),
        ).result
        result["client_request_id"] = request_id
        return result

    def _command_status(
        self,
        transport: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        claims = self._claims(transport)
        self._require_target(params, claims)
        method = self._required_text(params.get("method"), "method")
        if method not in _MUTATION_METHODS:
            raise _DispatchError(-32602, "invalid params")
        identity = CommandIdentity(
            session_key=claims["session_key"],
            user_id=claims["user_id"],
            provider=claims["provider"],
            client_instance_id=claims["client_instance_id"],
        )
        result = self._commands.status(
            identity,
            client_request_id=self._required_text(
                params.get("client_request_id"),
                "client_request_id",
            ),
            method=method,
        )
        if result is None:
            raise _DispatchError(
                CONTROL_ERROR_CODES["command_unknown"],
                "command_unknown",
            )
        value = result.result
        value["client_request_id"] = result.client_request_id
        return value

    def _binding(
        self,
        transport: Any,
        params: dict[str, Any],
    ) -> ControlBinding:
        claims = self._claims(transport)
        self._require_target(params, claims)
        runtime_session_id = params.get("runtime_session_id")
        if runtime_session_id is not None:
            runtime_session_id = self._required_text(
                runtime_session_id,
                "runtime_session_id",
            )
        transport_id = getattr(transport, "transport_id", None)
        if not isinstance(transport_id, str) or not transport_id:
            raise _DispatchError(
                CONTROL_ERROR_CODES["control_role_required"],
                "control_role_required",
            )
        binding = ControlBinding(
            session_key=claims["session_key"],
            profile=claims["profile"],
            runtime_generation=self._required_text(
                params.get("runtime_generation"),
                "runtime_generation",
            ),
            runtime_session_id=runtime_session_id,
            user_id=claims["user_id"],
            provider=claims["provider"],
            client_instance_id=claims["client_instance_id"],
            transport_id=transport_id,
        )
        if self._binding_validator is not None:
            self._binding_validator(binding)
        return binding

    @staticmethod
    def _claims(transport: Any) -> dict[str, str]:
        if getattr(transport, "connection_role", None) != "control":
            raise _DispatchError(
                CONTROL_ERROR_CODES["control_role_required"],
                "control_role_required",
            )
        value = getattr(transport, "auth_claims", None)
        if not isinstance(value, Mapping):
            raise _DispatchError(
                CONTROL_ERROR_CODES["control_role_required"],
                "control_role_required",
            )
        required = (
            "user_id",
            "provider",
            "client_instance_id",
            "session_key",
            "profile",
        )
        claims = {
            key: item
            for key in required
            if isinstance((item := value.get(key)), str) and bool(item)
        }
        if len(claims) != len(required):
            raise _DispatchError(
                CONTROL_ERROR_CODES["control_role_required"],
                "control_role_required",
            )
        return claims

    @staticmethod
    def _require_target(
        params: Mapping[str, Any],
        claims: Mapping[str, str],
    ) -> None:
        if (
            params.get("session_key", claims["session_key"]) != claims["session_key"]
            or params.get("profile", claims["profile"]) != claims["profile"]
        ):
            raise SessionBindingMismatch("session binding mismatch")

    @staticmethod
    def _lease_id(params: Mapping[str, Any]) -> str:
        value = params.get("lease_id")
        if not isinstance(value, str) or not value:
            raise LeaseRequired("controller lease required")
        return value

    @staticmethod
    def _required_text(value: object, _field: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise _DispatchError(-32602, "invalid params")
        return value

    @staticmethod
    def _rpc_error(request_id: object, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _error(error: Exception) -> tuple[int, str]:
        if isinstance(error, _DispatchError):
            return error.code, error.message
        error_mapping: tuple[
            tuple[type[Exception], str],
            ...,
        ] = (
            (ControllerConflict, "controller_conflict"),
            (LeaseRequired, "lease_required"),
            (LeaseExpired, "lease_expired"),
            (LeaseMismatch, "lease_mismatch"),
            (SessionBindingMismatch, "session_binding_mismatch"),
            (RequestPayloadConflict, "request_id_payload_conflict"),
            (CommandOwnershipMismatch, "lease_mismatch"),
            (OwnerActionMethodUnavailable, "method_not_allowed"),
        )
        for error_type, name in error_mapping:
            if isinstance(error, error_type):
                return CONTROL_ERROR_CODES[name], name
        if isinstance(error, ControlLeaseError):
            return CONTROL_ERROR_CODES["lease_mismatch"], "lease_mismatch"
        if isinstance(error, (TypeError, ValueError)):
            return -32602, "invalid params"
        return -32603, "internal error"


__all__ = [
    "ControlRequestDispatcher",
    "OwnerActionMethodUnavailable",
]
