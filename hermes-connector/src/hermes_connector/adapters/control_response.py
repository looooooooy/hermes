"""Platform-neutral result/error interpretation for the local control contract."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from hermes_connector.contracts.mobile_control import CONTROL_ERROR_REASONS
from hermes_connector.domain.control_command import (
    LocalControlFailure,
    LocalControlOutcomeUnknown,
)
from hermes_connector.domain.owner_control import (
    OwnerControlCallFailed,
    OwnerControlOutcomeUnknown,
)

_RETRYABLE_CODES = frozenset({4202, 4214, 4215})


def local_control_result(
    response: Mapping[str, object],
    *,
    effect_unknown: bool,
) -> Mapping[str, object]:
    fields = set(response)
    if "error" in response:
        if fields != {"jsonrpc", "id", "error"}:
            if effect_unknown:
                raise LocalControlOutcomeUnknown()
            raise LocalControlFailure("control_contract_unsupported")
        error = response["error"]
        if effect_unknown and not trusted_control_error(error):
            raise LocalControlOutcomeUnknown()
        raise local_control_error(error)
    if fields != {"jsonrpc", "id", "result"}:
        if effect_unknown:
            raise LocalControlOutcomeUnknown()
        raise LocalControlFailure("control_contract_unsupported")
    result = response["result"]
    if not isinstance(result, dict) or len(result) > 32:
        if effect_unknown:
            raise LocalControlOutcomeUnknown()
        raise LocalControlFailure("control_contract_unsupported")
    return MappingProxyType(dict(result))


def owner_control_result(
    response: Mapping[str, object],
    *,
    effect_unknown: bool,
) -> Mapping[str, object]:
    fields = set(response)
    if "error" in response:
        if fields != {"jsonrpc", "id", "error"}:
            if effect_unknown:
                raise OwnerControlOutcomeUnknown()
            raise OwnerControlCallFailed(4201, "control_contract_unsupported")
        raise owner_control_error(response["error"])
    if fields != {"jsonrpc", "id", "result"}:
        if effect_unknown:
            raise OwnerControlOutcomeUnknown()
        raise OwnerControlCallFailed(4201, "control_contract_unsupported")
    result = response["result"]
    if not isinstance(result, dict) or len(result) > 32:
        if effect_unknown:
            raise OwnerControlOutcomeUnknown()
        raise OwnerControlCallFailed(4201, "control_contract_unsupported")
    return MappingProxyType(dict(result))


def local_control_error(value: object) -> LocalControlFailure:
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        return LocalControlFailure("control_contract_unsupported")
    code = value["code"]
    if type(code) is not int or code not in CONTROL_ERROR_REASONS:
        return LocalControlFailure("internal_temporary", retryable=False)
    return LocalControlFailure(
        CONTROL_ERROR_REASONS[code],
        retryable=code in _RETRYABLE_CODES,
    )


def owner_control_error(value: object) -> OwnerControlCallFailed:
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        return OwnerControlCallFailed(4201, "control_contract_unsupported")
    code = value["code"]
    if type(code) is not int or code not in CONTROL_ERROR_REASONS:
        return OwnerControlCallFailed(4214, "owner_adapter_unavailable")
    return OwnerControlCallFailed(code, CONTROL_ERROR_REASONS[code])


def trusted_control_error(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"code", "message"}
        and type(value["code"]) is int
        and value["code"] in CONTROL_ERROR_REASONS
        and type(value["message"]) is str
    )


__all__ = [
    "local_control_error",
    "local_control_result",
    "owner_control_error",
    "owner_control_result",
    "trusted_control_error",
]
