"""Ephemeral Connector Gateway router for the owner-control lane."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.control.domain import ControlConnectorRoute


@dataclass(slots=True)
class _ConnectorBinding:
    route: ControlConnectorRoute
    connection_id: str
    connector_instance_id: str
    runtime_generation: str
    queue: asyncio.Queue[dict[str, object] | None]
    live: bool = True


@dataclass(slots=True)
class _TransportBinding:
    peer_id: str
    route: ControlConnectorRoute
    connector_connection_id: str
    live: bool = True


@dataclass(slots=True)
class _PendingRequest:
    peer_id: str
    route: ControlConnectorRoute
    connector_connection_id: str
    control_transport_id: str
    request_id: str
    operation: str
    fingerprint: str
    future: asyncio.Future[dict[str, object]]
    effect_started: bool = False
    orphaned: bool = False


class GatewayOwnerControlRouter:
    """Route bridge requests to one exact authenticated Connector connection."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        request_id_factory: Callable[[], UUID] = uuid4,
        max_in_flight: int = 64,
        max_transports: int = 64,
        disconnect_cleanup_seconds: float = 3.0,
    ) -> None:
        if type(max_in_flight) is not int or max_in_flight <= 0:
            raise ValueError("owner-control max in flight must be positive")
        if type(max_transports) is not int or max_transports <= 0:
            raise ValueError("owner-control max transports must be positive")
        if (
            not math.isfinite(disconnect_cleanup_seconds)
            or disconnect_cleanup_seconds <= 0
        ):
            raise ValueError("disconnect cleanup timeout must be positive")
        self._now = now
        self._request_id_factory = request_id_factory
        self._disconnect_cleanup_seconds = disconnect_cleanup_seconds
        self._max_in_flight = max_in_flight
        self._max_transports = max_transports
        self._queue_capacity = max_in_flight + max_transports
        self._connectors: dict[tuple[str, str], _ConnectorBinding] = {}
        self._transports: dict[str, _TransportBinding] = {}
        self._requests: dict[str, _PendingRequest] = {}
        self._parallel = asyncio.Semaphore(max_in_flight)
        self._lock = asyncio.Lock()

    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        route = _route(identity)
        async with self._lock:
            previous = self._connectors.get(_route_key(route))
            if previous is not None and previous.connection_id != connection_id:
                self._retire_connector_locked(previous)
            self._connectors[_route_key(route)] = _ConnectorBinding(
                route=route,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                queue=asyncio.Queue(maxsize=self._queue_capacity),
            )

    async def connector_disconnected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        route = _route(identity)
        async with self._lock:
            binding = self._connectors.get(_route_key(route))
            if (
                binding is None
                or binding.connection_id != connection_id
                or binding.connector_instance_id != connector_instance_id
            ):
                return
            del self._connectors[_route_key(route)]
            self._retire_connector_locked(binding)

    async def handle_bridge_request(
        self,
        *,
        peer_id: str,
        route: ControlConnectorRoute,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        request = _request_payload(payload)
        request_id = str(request["request_id"])
        control_transport_id = str(request["control_transport_id"])
        operation = str(request["operation"])
        fingerprint = _fingerprint(request)
        expires_at = _utc_instant(str(request["expires_at"]))
        remaining = (expires_at - self._aware_now()).total_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            return _failure(request, 4306, "deadline_exceeded_before_effect")

        async with self._parallel:
            async with self._lock:
                existing = self._requests.get(request_id)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        return _failure(
                            request,
                            4207,
                            "request_id_payload_conflict",
                        )
                    record = existing
                else:
                    binding = self._connectors.get(_route_key(route))
                    if binding is None or not binding.live:
                        return _failure(
                            request,
                            4202,
                            "live_runtime_unavailable",
                        )
                    if binding.queue.full():
                        return _failure(
                            request,
                            4215,
                            "relay_overloaded",
                        )
                    if operation == "control.transport.open":
                        if control_transport_id in self._transports:
                            return _failure(
                                request,
                                4207,
                                "request_id_payload_conflict",
                            )
                        if len(self._transports) >= self._max_transports:
                            return _failure(
                                request,
                                4215,
                                "relay_overloaded",
                            )
                        self._transports[control_transport_id] = _TransportBinding(
                            peer_id=peer_id,
                            route=route,
                            connector_connection_id=binding.connection_id,
                        )
                    else:
                        transport = self._transports.get(control_transport_id)
                        if (
                            transport is None
                            or not transport.live
                            or transport.peer_id != peer_id
                            or transport.route != route
                            or transport.connector_connection_id
                            != binding.connection_id
                        ):
                            return _failure(
                                request,
                                4202,
                                "live_runtime_unavailable",
                            )
                    record = _PendingRequest(
                        peer_id=peer_id,
                        route=route,
                        connector_connection_id=binding.connection_id,
                        control_transport_id=control_transport_id,
                        request_id=request_id,
                        operation=operation,
                        fingerprint=fingerprint,
                        future=asyncio.get_running_loop().create_future(),
                    )
                    self._requests[request_id] = record
                    binding.queue.put_nowait(request)

            try:
                response = await asyncio.wait_for(
                    asyncio.shield(record.future),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                await self._cancel_request(record)
                raise
            except TimeoutError:
                async with self._lock:
                    if not record.future.done():
                        record.future.set_result(
                            _failure(
                                request,
                                4307 if record.effect_started else 4306,
                                (
                                    "effect_unknown"
                                    if record.effect_started
                                    else "deadline_exceeded_before_effect"
                                ),
                                state=(
                                    "unknown" if record.effect_started else "failed"
                                ),
                            )
                        )
                response = await asyncio.shield(record.future)
            await self._finish_request(record, response)
            return dict(response)

    async def wait_for_control_request(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> Mapping[str, object] | None:
        route = _route(identity)
        async with self._lock:
            binding = self._connectors.get(_route_key(route))
            if not _exact_connector(
                binding,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            ):
                return None
            assert binding is not None
            queue = binding.queue
        return await queue.get()

    async def control_request_effect_started(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        request_id: str,
    ) -> bool:
        route = _route(identity)
        async with self._lock:
            record = self._requests.get(request_id)
            if (
                record is None
                or record.route != route
                or record.connector_connection_id != connection_id
                or record.future.done()
            ):
                return False
            record.effect_started = True
            return True

    async def accept_control_response(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        payload: Mapping[str, object],
    ) -> bool:
        route = _route(identity)
        response = _response_payload(payload)
        if response is None:
            return False
        request_id = str(response["request_id"])
        async with self._lock:
            binding = self._connectors.get(_route_key(route))
            if not _exact_connector(
                binding,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            ):
                return False
            record = self._requests.get(request_id)
            if (
                record is None
                or record.route != route
                or record.connector_connection_id != connection_id
                or record.control_transport_id != response["control_transport_id"]
                or record.operation != response["operation"]
                or record.future.done()
            ):
                return False
            record.future.set_result(response)
            if record.orphaned:
                self._requests.pop(record.request_id, None)
            return True

    async def bridge_disconnected(self, *, peer_id: str) -> None:
        async with self._lock:
            for record in self._requests.values():
                if record.peer_id != peer_id or record.future.done() or record.orphaned:
                    continue
                request = {
                    "request_id": record.request_id,
                    "control_transport_id": record.control_transport_id,
                    "operation": record.operation,
                }
                record.future.set_result(
                    _failure(
                        request,
                        4307 if record.effect_started else 4306,
                        (
                            "effect_unknown"
                            if record.effect_started
                            else "deadline_exceeded_before_effect"
                        ),
                        state="unknown" if record.effect_started else "failed",
                    )
                )
            transports = [
                (transport_id, transport)
                for transport_id, transport in self._transports.items()
                if transport.peer_id == peer_id and transport.live
            ]
            for transport_id, transport in transports:
                transport.live = False
                binding = self._connectors.get(_route_key(transport.route))
                if (
                    binding is None
                    or not binding.live
                    or binding.connection_id != transport.connector_connection_id
                ):
                    continue
                request_id = str(self._request_id_factory())
                issued_at = self._aware_now()
                request = {
                    "request_id": request_id,
                    "control_transport_id": transport_id,
                    "operation": "control.transport.close",
                    "issued_at": _utc_text(issued_at),
                    "expires_at": _utc_text(
                        issued_at
                        + timedelta(
                            seconds=self._disconnect_cleanup_seconds,
                        )
                    ),
                    "body": {"reason": "gateway_shutdown"},
                }
                self._requests[request_id] = _PendingRequest(
                    peer_id=peer_id,
                    route=transport.route,
                    connector_connection_id=binding.connection_id,
                    control_transport_id=transport_id,
                    request_id=request_id,
                    operation="control.transport.close",
                    fingerprint=_fingerprint(request),
                    future=asyncio.get_running_loop().create_future(),
                    orphaned=True,
                )
                binding.queue.put_nowait(request)

    def snapshot(self) -> dict[str, int]:
        """Return counts only; never expose control identifiers or payloads."""

        return {
            "live_connectors": sum(
                1 for binding in self._connectors.values() if binding.live
            ),
            "live_transports": sum(
                1 for transport in self._transports.values() if transport.live
            ),
            "tracked_requests": len(self._requests),
            "queued_requests": sum(
                binding.queue.qsize()
                for binding in self._connectors.values()
                if binding.live
            ),
            "max_in_flight": self._max_in_flight,
            "max_transports": self._max_transports,
        }

    async def _cancel_request(self, record: _PendingRequest) -> None:
        async with self._lock:
            self._requests.pop(record.request_id, None)
            if record.operation == "control.transport.open":
                self._transports.pop(record.control_transport_id, None)

    async def _finish_request(
        self,
        record: _PendingRequest,
        response: Mapping[str, object],
    ) -> None:
        async with self._lock:
            if record.operation == "control.transport.open" and (
                response.get("state") != "succeeded"
            ):
                self._transports.pop(record.control_transport_id, None)
            if record.operation == "control.transport.close":
                self._transports.pop(record.control_transport_id, None)
            if not record.orphaned:
                self._requests.pop(record.request_id, None)

    def _retire_connector_locked(self, binding: _ConnectorBinding) -> None:
        binding.live = False
        if not binding.queue.full():
            binding.queue.put_nowait(None)
        retired_transports: list[str] = []
        for transport_id, transport in self._transports.items():
            if (
                transport.route == binding.route
                and transport.connector_connection_id == binding.connection_id
            ):
                transport.live = False
                retired_transports.append(transport_id)
        for transport_id in retired_transports:
            self._transports.pop(transport_id, None)
        for record in self._requests.values():
            if (
                record.route != binding.route
                or record.connector_connection_id != binding.connection_id
                or record.future.done()
            ):
                continue
            request = {
                "request_id": record.request_id,
                "control_transport_id": record.control_transport_id,
                "operation": record.operation,
            }
            record.future.set_result(
                _failure(
                    request,
                    4307 if record.effect_started else 4306,
                    (
                        "effect_unknown"
                        if record.effect_started
                        else "deadline_exceeded_before_effect"
                    ),
                    state="unknown" if record.effect_started else "failed",
                )
            )
            if record.orphaned:
                self._requests.pop(record.request_id, None)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("owner-control clock must return aware datetime")
        return value.astimezone(UTC)


def _request_payload(payload: Mapping[str, object]) -> dict[str, object]:
    required = {
        "request_id",
        "control_transport_id",
        "operation",
        "issued_at",
        "expires_at",
        "body",
    }
    if set(payload) != required or not isinstance(payload.get("body"), Mapping):
        raise ValueError("invalid owner-control bridge request")
    request = dict(payload)
    for field in ("request_id", "control_transport_id"):
        _canonical_uuid(request[field], field=field)
    if not isinstance(request["operation"], str):
        raise TypeError("invalid owner-control operation")
    _utc_instant(str(request["issued_at"]))
    _utc_instant(str(request["expires_at"]))
    request["body"] = dict(request["body"])
    return request


def _response_payload(
    payload: Mapping[str, object],
) -> dict[str, object] | None:
    required = {
        "request_id",
        "control_transport_id",
        "operation",
        "state",
        "completed_at",
    }
    if not required <= set(payload):
        return None
    if payload.get("state") == "succeeded":
        if set(payload) != required | {"result"} or not isinstance(
            payload.get("result"), Mapping
        ):
            return None
    elif payload.get("state") in {"failed", "unknown"}:
        if set(payload) != required | {"error"} or not isinstance(
            payload.get("error"), Mapping
        ):
            return None
    else:
        return None
    try:
        _canonical_uuid(payload["request_id"], field="request_id")
        _canonical_uuid(
            payload["control_transport_id"],
            field="control_transport_id",
        )
        _utc_instant(str(payload["completed_at"]))
    except ValueError:
        return None
    return {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in payload.items()
    }


def _failure(
    request: Mapping[str, object],
    code: int,
    reason: str,
    *,
    state: str = "failed",
) -> dict[str, object]:
    return {
        "request_id": request["request_id"],
        "control_transport_id": request["control_transport_id"],
        "operation": request["operation"],
        "state": state,
        "completed_at": _utc_text(datetime.now(UTC)),
        "error": {"code": code, "reason": reason},
    }


def _route(identity: ConnectorIdentity) -> ControlConnectorRoute:
    return ControlConnectorRoute(identity.tenant_id, identity.device_id)


def _route_key(route: ControlConnectorRoute) -> tuple[str, str]:
    return route.tenant_id, route.device_id


def _exact_connector(
    binding: _ConnectorBinding | None,
    *,
    connection_id: str,
    connector_instance_id: str,
    runtime_generation: str,
) -> bool:
    return (
        binding is not None
        and binding.live
        and binding.connection_id == connection_id
        and binding.connector_instance_id == connector_instance_id
        and binding.runtime_generation == runtime_generation
    )


def _fingerprint(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_uuid(value: object, *, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be canonical UUID text")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{field} must be canonical UUID text") from error
    if str(parsed) != value:
        raise ValueError(f"{field} must be canonical UUID text")


def _utc_instant(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("owner-control timestamp must use UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("owner-control timestamp is invalid") from error
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace(
            "+00:00",
            "Z",
        )
    )
