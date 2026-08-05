"""Operation-scoped ORM adapter for the Connector command lane."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from threading import Event
from types import MappingProxyType
from typing import Any, Protocol
from uuid import RFC_4122, UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hermes_cloud.domain.connector_gateway import (
    ConnectorCommandDelivery,
    ConnectorIdentity,
)
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.platform.postgres.models import (
    ConnectorBindingModel,
    ControlCommandModel,
)


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


_TERMINAL_STATES = frozenset({"succeeded", "failed", "unknown", "expired"})
_METHODS = frozenset({"prompt.submit", "session.interrupt"})


class SqlAlchemyConnectorCommandRouter:
    """Persist dispatch, receipt and result facts before protocol cursors move."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        poll_interval_seconds: float = 0.1,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 0 < poll_interval_seconds <= 5:
            raise ValueError("command poll interval is outside bounds")
        self._session_factory = session_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._now = now
        self._late_registration_cleanups: set[asyncio.Task[None]] = set()

    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        cancellation_requested = Event()
        registration = asyncio.create_task(
            asyncio.to_thread(
                self._connector_connected_with_cancellation,
                identity,
                connection_id,
                connector_instance_id,
                runtime_generation,
                cancellation_requested,
            )
        )
        try:
            await asyncio.shield(registration)
        except asyncio.CancelledError:
            cancellation_requested.set()
            cleanup = asyncio.create_task(
                self._compensate_cancelled_registration(
                    registration,
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                )
            )
            self._late_registration_cleanups.add(cleanup)
            cleanup.add_done_callback(self._late_registration_cleanups.discard)
            raise

    async def _compensate_cancelled_registration(
        self,
        registration: asyncio.Task[None],
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        try:
            await asyncio.shield(registration)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - worker compensation is best effort.
            return
        try:
            await asyncio.to_thread(
                self._connector_disconnected,
                identity,
                connection_id,
                connector_instance_id,
            )
        except Exception:  # noqa: BLE001 - disconnect must not leak task errors.
            return

    def _connector_connected_with_cancellation(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        cancellation_requested: Event,
    ) -> None:
        try:
            self._connector_connected(
                identity,
                connection_id,
                connector_instance_id,
                runtime_generation,
            )
        finally:
            if cancellation_requested.is_set():
                self._connector_disconnected(
                    identity,
                    connection_id,
                    connector_instance_id,
                )

    async def connector_disconnected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._connector_disconnected,
            identity,
            connection_id,
            connector_instance_id,
        )

    async def wait_for_delivery(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorCommandDelivery | None:
        while True:
            delivery = await asyncio.to_thread(
                self._next_delivery,
                str(identity.tenant_id),
                str(identity.device_id),
                connection_id,
                connector_instance_id,
                runtime_generation,
                _utc(self._now()),
            )
            if delivery is not None:
                return delivery
            await asyncio.sleep(self._poll_interval_seconds)

    async def reserve_delivery(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        command_id: str,
        message_id: str,
        sequence: int,
    ) -> ConnectorCommandDelivery:
        return await asyncio.to_thread(
            self._reserve_delivery,
            identity,
            connection_id,
            connector_instance_id,
            command_id,
            message_id,
            sequence,
        )

    async def connector_heartbeat(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        await asyncio.to_thread(
            self._connector_heartbeat,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            next_connector_sequence,
            next_cloud_sequence,
        )

    async def accept_connector_response(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
    ) -> None:
        await asyncio.to_thread(
            self._accept_connector_response,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
        )

    def _connector_connected(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        _uuid(connection_id)
        _uuid(connector_instance_id)
        runtime_generation = _string(runtime_generation, 128)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            row = session.get(
                ConnectorBindingModel,
                (identity.tenant_id, identity.device_id),
            )
            if row is None:
                session.add(
                    ConnectorBindingModel(
                        tenant_id=identity.tenant_id,
                        device_id=identity.device_id,
                        connector_instance_id=connector_instance_id,
                        connection_id=connection_id,
                        runtime_generation=runtime_generation,
                        accepted_control=True,
                        state="active",
                        next_connector_sequence=0,
                        next_cloud_sequence=0,
                        revision=1,
                        connected_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.connector_instance_id = connector_instance_id
                row.connection_id = connection_id
                row.runtime_generation = runtime_generation
                row.accepted_control = True
                row.state = "active"
                row.next_connector_sequence = 0
                row.next_cloud_sequence = 0
                row.revision += 1
                row.connected_at = now
                row.updated_at = now
            session.flush()

    def _connector_disconnected(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            row = session.get(
                ConnectorBindingModel,
                (identity.tenant_id, identity.device_id),
            )
            if (
                row is not None
                and row.connection_id == connection_id
                and row.connector_instance_id == connector_instance_id
            ):
                row.state = "offline"
                row.revision += 1
                row.updated_at = now
                session.flush()

    def _connector_heartbeat(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        _sequence(next_connector_sequence)
        _sequence(next_cloud_sequence)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            row = session.get(
                ConnectorBindingModel,
                (identity.tenant_id, identity.device_id),
            )
            if (
                row is None
                or row.state != "active"
                or not row.accepted_control
                or row.connection_id != connection_id
                or row.connector_instance_id != connector_instance_id
                or row.runtime_generation != runtime_generation
            ):
                raise RuntimeError("Connector binding is no longer authoritative")
            if (
                next_connector_sequence < row.next_connector_sequence
                or next_cloud_sequence < row.next_cloud_sequence
            ):
                raise RuntimeError("Connector cursor projection regressed")
            row.next_connector_sequence = next_connector_sequence
            row.next_cloud_sequence = next_cloud_sequence
            row.revision += 1
            row.updated_at = now
            session.flush()

    def _next_delivery(
        self,
        tenant_id: str,
        device_id: str,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        now: datetime,
    ) -> ConnectorCommandDelivery | None:
        with self._session_factory.begin() as session:
            binding = session.get(
                ConnectorBindingModel,
                (tenant_id, device_id),
            )
            if (
                binding is None
                or binding.state != "active"
                or not binding.accepted_control
                or binding.connection_id != connection_id
                or binding.connector_instance_id != connector_instance_id
                or binding.runtime_generation != runtime_generation
            ):
                raise RuntimeError("Connector binding is no longer authoritative")
            expired_statement = select(ControlCommandModel).where(
                ControlCommandModel.tenant_id == tenant_id,
                ControlCommandModel.device_id == device_id,
                ControlCommandModel.connector_instance_id == connector_instance_id,
                ControlCommandModel.state.in_(("queued", "dispatched")),
                ControlCommandModel.expires_at <= now,
            )
            expired = session.execute(expired_statement).scalars().all()
            for row in expired:
                row.state = "expired"
                row.updated_at = now
            delivery_statement = (
                select(ControlCommandModel)
                .where(
                    ControlCommandModel.tenant_id == tenant_id,
                    ControlCommandModel.device_id == device_id,
                    ControlCommandModel.connector_instance_id == connector_instance_id,
                    ControlCommandModel.runtime_generation == runtime_generation,
                    ControlCommandModel.expires_at > now,
                    or_(
                        ControlCommandModel.state == "queued",
                        (
                            (ControlCommandModel.state == "dispatched")
                            & (
                                ControlCommandModel.dispatch_connection_id
                                != connection_id
                            )
                        ),
                    ),
                )
                .order_by(
                    ControlCommandModel.issued_at,
                    ControlCommandModel.command_id,
                )
                .limit(1)
            )
            row = session.execute(delivery_statement).scalar_one_or_none()
            session.flush()
            return _delivery(row) if row is not None else None

    def _reserve_delivery(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        command_id: str,
        message_id: str,
        sequence: int,
    ) -> ConnectorCommandDelivery:
        _uuid(command_id)
        _uuid(message_id)
        _sequence(sequence)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            binding = session.get(
                ConnectorBindingModel,
                (identity.tenant_id, identity.device_id),
            )
            if (
                binding is None
                or binding.state != "active"
                or not binding.accepted_control
                or binding.connection_id != connection_id
                or binding.connector_instance_id != connector_instance_id
            ):
                raise RuntimeError("Connector binding is no longer authoritative")
            row = session.get(
                ControlCommandModel,
                (identity.tenant_id, command_id),
            )
            if (
                row is None
                or row.delivery_message_id != message_id
                or row.device_id != identity.device_id
                or row.connector_instance_id != connector_instance_id
                or row.runtime_generation != binding.runtime_generation
            ):
                raise RuntimeError("dispatch reservation binding changed")
            if row.expires_at <= now:
                if row.state not in _TERMINAL_STATES:
                    row.state = "expired"
                    row.updated_at = now
                    session.flush()
                raise RuntimeError("dispatch reservation expired")
            if (
                row.state == "dispatched"
                and row.dispatch_connection_id == connection_id
                and row.dispatch_sequence == sequence
            ):
                return _delivery(row)
            if row.state not in {"queued", "dispatched"}:
                raise RuntimeError("command is not dispatchable")
            row.state = "dispatched"
            row.dispatch_connection_id = connection_id
            row.dispatch_sequence = sequence
            if row.delivery_sent_at is None:
                row.delivery_sent_at = now
            row.dispatched_at = now
            row.updated_at = now
            session.flush()
            return _delivery(row)

    def _accept_connector_response(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
    ) -> None:
        if envelope.message_type not in {"command.receipt", "command.result"}:
            raise RuntimeError("connector response type is unsupported")
        if (
            envelope.tenant_id != identity.tenant_id
            or envelope.device_id != identity.device_id
        ):
            raise RuntimeError("connector response identity changed")
        _uuid(envelope.message_id)
        payload = _response_payload(envelope.message_type, envelope.payload)
        digest = _payload_digest(envelope.message_type, payload)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            binding = session.get(
                ConnectorBindingModel,
                (identity.tenant_id, identity.device_id),
            )
            if (
                binding is None
                or binding.state != "active"
                or not binding.accepted_control
                or binding.connection_id != connection_id
                or binding.connector_instance_id != connector_instance_id
                or binding.runtime_generation != runtime_generation
            ):
                raise RuntimeError("Connector binding is no longer authoritative")
            row = session.get(
                ControlCommandModel,
                (identity.tenant_id, payload["command_id"]),
            )
            if (
                row is None
                or row.dispatch_connection_id != connection_id
                or row.connector_instance_id != connector_instance_id
                or row.runtime_generation != runtime_generation
            ):
                raise RuntimeError("connector response dispatch binding changed")
            _response_binding(row, payload)
            if envelope.message_type == "command.receipt":
                self._apply_receipt(
                    row,
                    envelope.message_id,
                    payload,
                    digest,
                    now,
                )
            else:
                self._apply_result(
                    row,
                    envelope.message_id,
                    payload,
                    digest,
                    now,
                )
            session.flush()

    @staticmethod
    def _apply_receipt(
        row: ControlCommandModel,
        envelope_message_id: str,
        payload: Mapping[str, Any],
        digest: str,
        now: datetime,
    ) -> None:
        if row.receipt_message_id is not None:
            if (
                row.receipt_message_id != envelope_message_id
                or row.receipt_digest != digest
            ):
                raise RuntimeError("connector receipt conflicts with stored fact")
            return
        if (
            row.state != "dispatched"
            or payload["message_id"] != row.delivery_message_id
            or payload["state"] != "delivered"
            or payload["revision"] != row.revision
        ):
            raise RuntimeError("connector receipt transition is invalid")
        row.receipt_message_id = envelope_message_id
        row.receipt_digest = digest
        row.state = "delivered"
        row.delivered_at = _instant(payload["stored_at"])
        row.updated_at = now

    @staticmethod
    def _apply_result(
        row: ControlCommandModel,
        envelope_message_id: str,
        payload: Mapping[str, Any],
        digest: str,
        now: datetime,
    ) -> None:
        if row.result_message_id is not None:
            if (
                row.result_message_id != envelope_message_id
                or row.result_digest != digest
            ):
                raise RuntimeError("connector result conflicts with stored fact")
            return
        if row.state != "delivered" or payload["revision"] <= row.revision:
            raise RuntimeError("connector result transition is invalid")
        row.result_message_id = envelope_message_id
        row.result_digest = digest
        row.state = str(payload["state"])
        row.revision = int(payload["revision"])
        row.result = payload.get("result")
        row.error = payload.get("error")
        row.completed_at = _instant(payload["completed_at"])
        row.updated_at = now


def _delivery(row: ControlCommandModel) -> ConnectorCommandDelivery:
    sent_at = row.delivery_sent_at or row.issued_at
    return ConnectorCommandDelivery(
        command_id=row.command_id,
        message_id=row.delivery_message_id,
        sent_at=_instant_text(sent_at),
        payload=MappingProxyType(
            {
                "command_id": row.command_id,
                "connector_instance_id": row.connector_instance_id,
                "client_instance_id": row.client_instance_id,
                "session_key": row.session_key,
                "profile": row.profile,
                "client_request_id": row.client_request_id,
                "method": row.method,
                "params": dict(row.params),
                "issued_at": _instant_text(row.issued_at),
                "expires_at": _instant_text(row.expires_at),
                "revision": row.revision,
            }
        ),
    )


def _response_payload(message_type: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("connector response payload is invalid")
    common = {
        "command_id",
        "connector_instance_id",
        "client_instance_id",
        "session_key",
        "profile",
        "client_request_id",
        "method",
    }
    if message_type == "command.receipt":
        expected = common | {
            "message_id",
            "state",
            "stored_at",
            "revision",
        }
    else:
        expected = common | {"state", "completed_at", "revision"}
        expected |= {"result"} if value.get("state") == "succeeded" else {"error"}
    if set(value) != expected:
        raise RuntimeError("connector response fields are invalid")
    payload = dict(value)
    for field in ("command_id", "connector_instance_id", "client_instance_id"):
        _uuid(payload[field])
    if message_type == "command.receipt":
        _uuid(payload["message_id"])
        if payload["state"] != "delivered":
            raise RuntimeError("connector receipt state is invalid")
        _instant(payload["stored_at"])
    else:
        if payload["state"] not in {"succeeded", "failed", "unknown"}:
            raise RuntimeError("connector result state is invalid")
        _instant(payload["completed_at"])
        if payload["state"] == "succeeded":
            _success_result(payload)
        else:
            _error_result(payload)
    for field, maximum in (
        ("session_key", 256),
        ("profile", 128),
        ("client_request_id", 128),
        ("method", 64),
    ):
        _string(payload[field], maximum)
    if payload["method"] not in _METHODS:
        raise RuntimeError("connector response method is invalid")
    if type(payload["revision"]) is not int or payload["revision"] < 1:
        raise RuntimeError("connector response revision is invalid")
    return payload


def _success_result(payload: Mapping[str, Any]) -> None:
    result = payload["result"]
    if not isinstance(result, dict):
        raise TypeError("connector success result is invalid")
    if payload["method"] == "prompt.submit":
        required = {"status", "client_request_id", "client_turn_id"}
        if not required <= set(result) or set(result) - required - {"server_turn_id"}:
            raise RuntimeError("connector prompt result is invalid")
        _string(result["client_turn_id"], 128)
        if "server_turn_id" in result:
            _string(result["server_turn_id"], 128)
    elif set(result) != {"status", "client_request_id"}:
        raise RuntimeError("connector interrupt result is invalid")
    if result["status"] not in {"accepted", "queued", "rejected"}:
        raise RuntimeError("connector command status is invalid")
    if result["client_request_id"] != payload["client_request_id"]:
        raise RuntimeError("connector client request binding changed")


def _error_result(payload: Mapping[str, Any]) -> None:
    error = payload["error"]
    if not isinstance(error, dict) or set(error) != {
        "code",
        "message",
        "retryable",
    }:
        raise RuntimeError("connector command error is invalid")
    _string(error["code"], 128)
    _string(error["message"], 512)
    if type(error["retryable"]) is not bool:
        raise RuntimeError("connector command retryability is invalid")


def _response_binding(
    row: ControlCommandModel,
    payload: Mapping[str, Any],
) -> None:
    expected = {
        "command_id": row.command_id,
        "connector_instance_id": row.connector_instance_id,
        "client_instance_id": row.client_instance_id,
        "session_key": row.session_key,
        "profile": row.profile,
        "client_request_id": row.client_request_id,
        "method": row.method,
    }
    if any(payload[field] != value for field, value in expected.items()):
        raise RuntimeError("connector response binding changed")


def _payload_digest(message_type: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"message_type": message_type, "payload": payload},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("canonical UUID is required")
    try:
        parsed = UUID(value)
    except ValueError:
        raise RuntimeError("canonical UUID is required") from None
    if (
        str(parsed) != value
        or parsed.variant != RFC_4122
        or parsed.version not in {1, 2, 3, 4, 5}
    ):
        raise RuntimeError("canonical UUID is required")
    return value


def _string(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or "\x00" in value
    ):
        raise RuntimeError("bounded protocol text is required")
    return value


def _sequence(value: object) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeError("non-negative protocol sequence is required")
    return value


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise RuntimeError("router clock must be timezone-aware")
    return value.astimezone(UTC)


def _instant(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeError("UTC instant is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RuntimeError("UTC instant is invalid") from None
    return _utc(parsed)


def _instant_text(value: datetime) -> str:
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")
