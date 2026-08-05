"""Frozen Control v1 method and error-code constants."""

from __future__ import annotations

from uuid import RFC_4122, UUID


def is_canonical_client_instance_id(value: object) -> bool:
    """Return whether *value* is the exact lowercase hyphenated UUID form."""
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return value == str(parsed) and parsed.variant == RFC_4122
    except (AttributeError, TypeError, ValueError):
        return False


CONTROL_CONTRACT_VERSION = 1
CONTROL_V1_ERROR_RANGE = range(4200, 4220)

CONTROL_METHODS = frozenset(
    {
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "session.command.status",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "session.redirect",
        "approval.respond",
        "clarify.respond",
        "sudo.respond",
        "secret.respond",
        "terminal.read.respond",
    }
)

# Reserved methods are not automatically executable. Only mutations with
# complete transport binding, lease, idempotency, and owner-adapter enforcement
# belong here. Future methods stay fail-closed until their wrapper is complete.
CONTROL_AVAILABLE_METHODS = frozenset(
    {
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "session.command.status",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    }
)

CONTROL_ERROR_CODES = {
    "control_role_required": 4200,
    "control_contract_unsupported": 4201,
    "live_runtime_unavailable": 4202,
    "controller_conflict": 4203,
    "lease_required": 4204,
    "lease_expired": 4205,
    "lease_mismatch": 4206,
    "request_id_payload_conflict": 4207,
    "pending_request_conflict": 4208,
    "method_not_allowed": 4209,
    "command_unknown": 4210,
    "revision_conflict": 4211,
    "session_binding_mismatch": 4212,
    "invalid_pending_response": 4213,
    "owner_adapter_unavailable": 4214,
    "relay_overloaded": 4215,
}

CONTROL_ROLE_REQUIRED = CONTROL_ERROR_CODES["control_role_required"]
CONTROL_CONTRACT_UNSUPPORTED = CONTROL_ERROR_CODES["control_contract_unsupported"]
LIVE_RUNTIME_UNAVAILABLE = CONTROL_ERROR_CODES["live_runtime_unavailable"]
CONTROLLER_CONFLICT = CONTROL_ERROR_CODES["controller_conflict"]
LEASE_REQUIRED = CONTROL_ERROR_CODES["lease_required"]
LEASE_EXPIRED = CONTROL_ERROR_CODES["lease_expired"]
LEASE_MISMATCH = CONTROL_ERROR_CODES["lease_mismatch"]
REQUEST_ID_PAYLOAD_CONFLICT = CONTROL_ERROR_CODES["request_id_payload_conflict"]
PENDING_REQUEST_CONFLICT = CONTROL_ERROR_CODES["pending_request_conflict"]
METHOD_NOT_ALLOWED = CONTROL_ERROR_CODES["method_not_allowed"]
COMMAND_UNKNOWN = CONTROL_ERROR_CODES["command_unknown"]
REVISION_CONFLICT = CONTROL_ERROR_CODES["revision_conflict"]
SESSION_BINDING_MISMATCH = CONTROL_ERROR_CODES["session_binding_mismatch"]
INVALID_PENDING_RESPONSE = CONTROL_ERROR_CODES["invalid_pending_response"]
OWNER_ADAPTER_UNAVAILABLE = CONTROL_ERROR_CODES["owner_adapter_unavailable"]
RELAY_OVERLOADED = CONTROL_ERROR_CODES["relay_overloaded"]

__all__ = [
    "COMMAND_UNKNOWN",
    "CONTROLLER_CONFLICT",
    "CONTROL_AVAILABLE_METHODS",
    "CONTROL_CONTRACT_UNSUPPORTED",
    "CONTROL_CONTRACT_VERSION",
    "CONTROL_ERROR_CODES",
    "CONTROL_METHODS",
    "CONTROL_ROLE_REQUIRED",
    "CONTROL_V1_ERROR_RANGE",
    "INVALID_PENDING_RESPONSE",
    "LEASE_EXPIRED",
    "LEASE_MISMATCH",
    "LEASE_REQUIRED",
    "LIVE_RUNTIME_UNAVAILABLE",
    "METHOD_NOT_ALLOWED",
    "OWNER_ADAPTER_UNAVAILABLE",
    "PENDING_REQUEST_CONFLICT",
    "RELAY_OVERLOADED",
    "REQUEST_ID_PAYLOAD_CONFLICT",
    "REVISION_CONFLICT",
    "SESSION_BINDING_MISMATCH",
    "is_canonical_client_instance_id",
]
