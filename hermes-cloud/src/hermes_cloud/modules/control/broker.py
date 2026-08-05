"""Process-local owner-control routing with no persistence dependencies."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRpcError,
)
from hermes_cloud.modules.control.ports import ControlRequestSenderPort

_LIVE_RUNTIME_UNAVAILABLE = (4202, "authoritative live runtime unavailable")
_REQUEST_PAYLOAD_CONFLICT = (4207, "request id payload conflict")
_DEADLINE_BEFORE_EFFECT = (4306, "deadline exceeded before effect")
_EFFECT_UNKNOWN = (4307, "effect unknown")


@dataclass(frozen=True, slots=True)
class _Outcome:
    result: dict[str, object] | None = None
    error: tuple[int, str] | None = None


@dataclass(slots=True)
class _RequestRecord:
    fingerprint: str
    operation: str
    future: asyncio.Future[_Outcome]
    effect_started: bool = False


@dataclass(slots=True)
class _ConnectorBinding:
    route: ControlConnectorRoute
    connection_id: str
    sender: ControlRequestSenderPort


@dataclass(slots=True)
class _ControlTransport:
    cloud_connection_id: str
    control_transport_id: str
    route: ControlConnectorRoute
    connector_connection_id: str
    requests: dict[str, _RequestRecord] = field(default_factory=dict)
    live: bool = True


class OwnerControlBroker:
    """Correlate ephemeral control transports and matching responses in memory."""

    def __init__(
        self,
        *,
        control_transport_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._control_transport_id_factory = control_transport_id_factory
        self._connectors: dict[
            tuple[str, str],
            _ConnectorBinding,
        ] = {}
        self._transports: dict[str, _ControlTransport] = {}
        self._lock = asyncio.Lock()

    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connector_connection_id: str,
        sender: ControlRequestSenderPort,
    ) -> None:
        route = _route(identity)
        _canonical_uuid(connector_connection_id, field="connector connection id")
        async with self._lock:
            previous = self._connectors.get(_route_key(route))
            if (
                previous is not None
                and previous.connection_id != connector_connection_id
            ):
                self._retire_binding_locked(previous)
            self._connectors[_route_key(route)] = _ConnectorBinding(
                route=route,
                connection_id=connector_connection_id,
                sender=sender,
            )

    async def connector_disconnected(
        self,
        *,
        identity: ConnectorIdentity,
        connector_connection_id: str,
    ) -> None:
        route = _route(identity)
        async with self._lock:
            binding = self._connectors.get(_route_key(route))
            if binding is None or binding.connection_id != connector_connection_id:
                return
            del self._connectors[_route_key(route)]
            self._retire_binding_locked(binding)

    async def open_transport(
        self,
        *,
        control_connection_id: str,
        route: ControlConnectorRoute,
        request_id: str,
        issued_at: str,
        expires_at: str,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        _canonical_uuid(control_connection_id, field="control connection id")
        control_transport_id = str(self._control_transport_id_factory())
        _canonical_uuid(control_transport_id, field="control transport id")
        async with self._lock:
            if control_connection_id in self._transports:
                raise ControlRpcError(
                    code=_REQUEST_PAYLOAD_CONFLICT[0],
                    message=_REQUEST_PAYLOAD_CONFLICT[1],
                )
            connector = self._connectors.get(_route_key(route))
            if connector is None:
                raise ControlRpcError(
                    code=_LIVE_RUNTIME_UNAVAILABLE[0],
                    message=_LIVE_RUNTIME_UNAVAILABLE[1],
                )
            self._transports[control_connection_id] = _ControlTransport(
                cloud_connection_id=control_connection_id,
                control_transport_id=control_transport_id,
                route=route,
                connector_connection_id=connector.connection_id,
            )
        try:
            return await self.route_request(
                control_connection_id=control_connection_id,
                request_id=request_id,
                operation="control.transport.open",
                issued_at=issued_at,
                expires_at=expires_at,
                body=body,
                timeout_seconds=timeout_seconds,
            )
        except BaseException:
            async with self._lock:
                self._transports.pop(control_connection_id, None)
            raise

    async def close_transport(
        self,
        *,
        control_connection_id: str,
        request_id: str,
        issued_at: str,
        expires_at: str,
        reason: str,
        timeout_seconds: float,
    ) -> None:
        async with self._lock:
            transport = self._transports.get(control_connection_id)
            should_route = transport is not None and self._binding_is_live_locked(
                transport
            )
        try:
            if should_route:
                await self.route_request(
                    control_connection_id=control_connection_id,
                    request_id=request_id,
                    operation="control.transport.close",
                    issued_at=issued_at,
                    expires_at=expires_at,
                    body={"reason": reason},
                    timeout_seconds=timeout_seconds,
                )
        finally:
            async with self._lock:
                self._transports.pop(control_connection_id, None)

    async def route_request(
        self,
        *,
        control_connection_id: str,
        request_id: str,
        operation: str,
        issued_at: str,
        expires_at: str,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        _canonical_uuid(request_id, field="control request id")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("control request timeout must be finite and positive")
        payload = {
            "request_id": request_id,
            "control_transport_id": "",
            "operation": operation,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "body": dict(body),
        }
        fingerprint = _fingerprint(
            {
                "operation": operation,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "body": dict(body),
            }
        )
        owner = False
        async with self._lock:
            transport = self._transports.get(control_connection_id)
            if transport is None or not self._binding_is_live_locked(transport):
                raise ControlRpcError(
                    code=_LIVE_RUNTIME_UNAVAILABLE[0],
                    message=_LIVE_RUNTIME_UNAVAILABLE[1],
                )
            payload["control_transport_id"] = transport.control_transport_id
            record = transport.requests.get(request_id)
            if record is not None:
                if record.fingerprint != fingerprint:
                    raise ControlRpcError(
                        code=_REQUEST_PAYLOAD_CONFLICT[0],
                        message=_REQUEST_PAYLOAD_CONFLICT[1],
                    )
            else:
                record = _RequestRecord(
                    fingerprint=fingerprint,
                    operation=operation,
                    future=asyncio.get_running_loop().create_future(),
                )
                transport.requests[request_id] = record
                owner = True
            connector = self._connectors[_route_key(transport.route)]

        if owner:
            await self._send_owner_request(
                transport=transport,
                connector=connector,
                record=record,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
        return await self._await_outcome(record, timeout_seconds=timeout_seconds)

    async def accept_control_response(
        self,
        *,
        identity: ConnectorIdentity,
        connector_connection_id: str,
        response: Mapping[str, object],
    ) -> bool:
        route = _route(identity)
        request_id = response.get("request_id")
        control_transport_id = response.get("control_transport_id")
        operation = response.get("operation")
        state = response.get("state")
        if not all(
            isinstance(value, str)
            for value in (
                request_id,
                control_transport_id,
                operation,
                state,
            )
        ):
            return False
        async with self._lock:
            binding = self._connectors.get(_route_key(route))
            if binding is None or binding.connection_id != connector_connection_id:
                return False
            transport = next(
                (
                    candidate
                    for candidate in self._transports.values()
                    if candidate.control_transport_id == control_transport_id
                    and candidate.connector_connection_id == connector_connection_id
                    and _route_key(candidate.route) == _route_key(route)
                    and candidate.live
                ),
                None,
            )
            if transport is None:
                return False
            record = transport.requests.get(request_id)
            if record is None or record.operation != operation or record.future.done():
                return False
            outcome = _response_outcome(response, state=state)
            if outcome is None:
                return False
            record.future.set_result(outcome)
            return True

    def control_transport_id_for(
        self,
        control_connection_id: str,
    ) -> str | None:
        transport = self._transports.get(control_connection_id)
        return transport.control_transport_id if transport is not None else None

    async def _send_owner_request(
        self,
        *,
        transport: _ControlTransport,
        connector: _ConnectorBinding,
        record: _RequestRecord,
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> None:
        try:
            effect_started = await asyncio.wait_for(
                connector.sender.send_control_request(payload),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._finish_if_pending(record, error=_EFFECT_UNKNOWN)
            return
        except Exception:  # noqa: BLE001 - injected bridge failures stay opaque
            await self._finish_if_pending(record, error=_EFFECT_UNKNOWN)
            return
        async with self._lock:
            if not record.future.done():
                record.effect_started = bool(effect_started)
            if not self._binding_is_live_locked(transport) and not record.future.done():
                record.future.set_result(
                    _Outcome(
                        error=(
                            _EFFECT_UNKNOWN
                            if record.effect_started
                            else _DEADLINE_BEFORE_EFFECT
                        )
                    )
                )
        if not effect_started:
            await self._finish_if_pending(
                record,
                error=_DEADLINE_BEFORE_EFFECT,
            )

    async def _await_outcome(
        self,
        record: _RequestRecord,
        *,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(record.future),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await self._finish_if_pending(
                record,
                error=(
                    _EFFECT_UNKNOWN
                    if record.effect_started
                    else _DEADLINE_BEFORE_EFFECT
                ),
            )
            outcome = await asyncio.shield(record.future)
        if outcome.error is not None:
            raise ControlRpcError(
                code=outcome.error[0],
                message=outcome.error[1],
            )
        assert outcome.result is not None
        return dict(outcome.result)

    async def _finish_if_pending(
        self,
        record: _RequestRecord,
        *,
        error: tuple[int, str],
    ) -> None:
        async with self._lock:
            if not record.future.done():
                record.future.set_result(_Outcome(error=error))

    def _binding_is_live_locked(self, transport: _ControlTransport) -> bool:
        connector = self._connectors.get(_route_key(transport.route))
        return (
            transport.live
            and connector is not None
            and connector.connection_id == transport.connector_connection_id
        )

    def _retire_binding_locked(self, binding: _ConnectorBinding) -> None:
        for transport in self._transports.values():
            if (
                _route_key(transport.route) != _route_key(binding.route)
                or transport.connector_connection_id != binding.connection_id
            ):
                continue
            transport.live = False
            for record in transport.requests.values():
                if record.future.done():
                    continue
                record.future.set_result(
                    _Outcome(
                        error=(
                            _EFFECT_UNKNOWN
                            if record.effect_started
                            else _DEADLINE_BEFORE_EFFECT
                        )
                    )
                )


def _route(identity: ConnectorIdentity) -> ControlConnectorRoute:
    return ControlConnectorRoute(
        tenant_id=identity.tenant_id,
        device_id=identity.device_id,
    )


def _route_key(route: ControlConnectorRoute) -> tuple[str, str]:
    return route.tenant_id, route.device_id


def _canonical_uuid(value: str, *, field: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be canonical UUID text") from error
    if str(parsed) != value:
        raise ValueError(f"{field} must be canonical UUID text")


def _fingerprint(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("control request payload must be JSON") from error


def _response_outcome(
    response: Mapping[str, object],
    *,
    state: str,
) -> _Outcome | None:
    if state == "succeeded":
        result = response.get("result")
        if not isinstance(result, Mapping):
            return None
        return _Outcome(result=dict(result))
    if state not in {"failed", "unknown"}:
        return None
    error = response.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    reason = error.get("reason")
    if type(code) is not int or not isinstance(reason, str):
        return None
    if state == "unknown" and (code, reason) != (4307, "effect_unknown"):
        return None
    return _Outcome(error=(code, reason.replace("_", " ")))
