from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from hermes_connector.domain.cloud_protocol import (
    CommandDelivery,
    CommandReceipt,
    CommandResult,
    ConnectorHeartbeat,
    ConnectorHello,
    ConnectorWelcome,
)
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.observer import (
    SessionEvent,
    SessionObserveClose,
    SessionObserveOpen,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)
from hermes_connector.domain.owner_control import (
    OwnerControlRequest,
    OwnerControlResponse,
)
from hermes_connector.domain.session_catalog import (
    SessionCatalogAck,
    SessionCatalogEvent,
    SessionCatalogNack,
    SessionCatalogSnapshotPage,
)


class CloudCredentialUnavailable(ValueError):
    """Cloud credential material is absent, malformed, or unavailable."""


class CloudConnectionClosed(ConnectionError):
    """A remote WebSocket close with its policy-relevant metadata preserved."""

    __slots__ = ("code", "reason")

    def __init__(self, *, code: int, reason: str) -> None:
        super().__init__("cloud WebSocket was closed")
        self.code = code
        self.reason = reason


class CloudConnectionPort(Protocol):
    async def send(self, frame: bytes, *, timeout_seconds: float) -> None:
        """Send one binary Cloud frame.

        Input/unit: ``frame`` bytes and positive ``timeout_seconds`` seconds.
        Deadline: bounded by ``timeout_seconds``. Idempotency: none at transport;
        the caller's durable sequence is the retry key. Effect: one socket write.
        Return: ``None`` after transport acceptance. Errors: timeout, connection,
        operating-system, cancellation, or invalid-frame failures.
        """

    async def receive(self, *, timeout_seconds: float) -> bytes:
        """Receive one binary Cloud frame.

        Input/unit: positive ``timeout_seconds`` seconds.
        Deadline: bounded by that value. Idempotency: not applicable to stream reads.
        Effect: consumes at most one transport frame, with no durable business effect.
        Return: frame bytes. Errors: timeout, connection, operating-system,
        cancellation, or invalid-frame failures.
        """

    async def close(
        self,
        *,
        code: int,
        reason: str,
        timeout_seconds: float,
    ) -> None:
        """Close the WebSocket session.

        Input/unit: WebSocket integer ``code``, UTF-8 ``reason``, timeout in seconds.
        Deadline: bounded by ``timeout_seconds``. Idempotency: repeated close is safe.
        Effect: closes transport resources only. Return: ``None`` after the attempt.
        Errors: timeout, connection, operating-system, or cancellation failures.
        """


class CloudTransportPort(Protocol):
    async def connect(
        self,
        endpoint: str,
        *,
        token: str,
    ) -> CloudConnectionPort:
        """Open one authenticated Cloud connection.

        Input/unit: absolute WSS ``endpoint`` and opaque bearer ``token``.
        Deadline: the adapter's configured open deadline in seconds.
        Idempotency: none; each call may create a socket. Effect: network/auth I/O.
        Return: an open connection. Errors: timeout, TLS, authentication,
        connection, operating-system, cancellation, or invalid-endpoint failures.
        """


class CloudTokenProviderPort(Protocol):
    async def access_token(self) -> str:
        """Load an opaque Cloud access token.

        Input/unit: none. Deadline: provider-defined bounded retrieval deadline.
        Idempotency: repeatable lookup; provider refresh may update secure storage.
        Effect: secure-token read/refresh only; never logging token content.
        Return: non-empty token text. Errors: provider, authentication, timeout,
        secure-storage, or cancellation failures.
        """

    async def clear_access_token(self) -> None:
        """Remove locally cached Cloud authentication.

        Input/unit: none. Deadline: provider-defined bounded storage deadline.
        Idempotency: repeated clearing is safe. Effect: deletes cached token material.
        Return: ``None`` when absent/removed. Errors: provider, secure-storage,
        timeout, or cancellation failures.
        """


@runtime_checkable
class CloudLifecycleTokenProviderPort(Protocol):
    async def apply_lifecycle_signal(self, signal: str) -> None:
        """Persist an explicit server lifecycle state and clear cached auth."""


class ConnectorProtocolCodecPort(Protocol):
    def decode_envelope(self, frame: bytes) -> CloudEnvelope:
        """Decode one Cloud envelope.

        Input/unit: binary frame bytes, at most 262144 bytes.
        Deadline: none; bounded in-memory work. Idempotency/effect: deterministic
        and side-effect free. Return: validated ``CloudEnvelope``.
        Errors: UTF-8, JSON, size, duplicate-key, limit, or schema violations.
        """

    def encode_envelope(self, message: CloudEnvelope) -> bytes:
        """Encode one Cloud envelope.

        Input/unit: one validated ``CloudEnvelope``.
        Deadline: none; bounded in-memory work. Idempotency/effect: deterministic
        and side-effect free. Return: canonical frame bytes up to 262144 bytes.
        Errors: size, value, nesting, collection, or schema violations.
        """

    def decode_command_delivery_payload(
        self,
        payload: Mapping[str, object],
    ) -> CommandDelivery: ...

    def decode_command_receipt_payload(
        self,
        payload: Mapping[str, object],
    ) -> CommandReceipt: ...

    def decode_command_result_payload(
        self,
        payload: Mapping[str, object],
    ) -> CommandResult: ...

    def decode_command_receipt(self, frame: bytes) -> CommandReceipt: ...

    def decode_command_result(self, frame: bytes) -> CommandResult: ...

    def command_receipt_payload(
        self,
        message: CommandReceipt,
    ) -> Mapping[str, object]: ...

    def command_result_payload(
        self,
        message: CommandResult,
    ) -> Mapping[str, object]: ...

    def decode_control_request_payload(
        self,
        payload: Mapping[str, object],
    ) -> OwnerControlRequest: ...

    def decode_control_response_payload(
        self,
        payload: Mapping[str, object],
    ) -> OwnerControlResponse: ...

    def control_response_payload(
        self,
        message: OwnerControlResponse,
    ) -> Mapping[str, object]: ...

    def decode_session_observe_open_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveOpen: ...

    def decode_session_observe_open_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveOpen: ...

    def decode_session_observe_close_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveClose: ...

    def decode_session_observe_close_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveClose: ...

    def decode_stream_ack_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamAck: ...

    def decode_stream_ack_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamAck: ...

    def decode_stream_nack_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamNack: ...

    def decode_stream_nack_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamNack: ...

    def session_snapshot_payload(
        self,
        message: SessionSnapshot,
    ) -> Mapping[str, object]: ...

    def session_event_payload(
        self,
        message: SessionEvent,
    ) -> Mapping[str, object]: ...

    def decode_session_catalog_ack_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionCatalogAck: ...

    def decode_session_catalog_nack_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionCatalogNack: ...

    def session_catalog_snapshot_page_payload(
        self,
        message: SessionCatalogSnapshotPage,
    ) -> Mapping[str, object]: ...

    def session_catalog_event_payload(
        self,
        message: SessionCatalogEvent,
    ) -> Mapping[str, object]: ...

    def hello_payload(
        self,
        message: ConnectorHello,
    ) -> Mapping[str, object]:
        """Map a Connector Hello to its payload object.

        Input/unit: one ``ConnectorHello``. Deadline: none; bounded in-memory work.
        Idempotency/effect: deterministic and side-effect free.
        Return: immutable-compatible payload fields. Errors: invalid value or limit
        violations; no transport or persistence errors.
        """

    def decode_hello_payload(
        self,
        payload: Mapping[str, object],
    ) -> ConnectorHello:
        """Validate and decode a Connector Hello payload.

        Input/unit: payload mapping with bounded protocol values.
        Deadline: none; bounded in-memory work. Idempotency/effect: deterministic
        and side-effect free. Return: ``ConnectorHello``.
        Errors: missing, unknown, malformed, or contract-invalid fields.
        """

    def decode_welcome_payload(
        self,
        payload: Mapping[str, object],
    ) -> ConnectorWelcome:
        """Validate and decode a Connector Welcome payload.

        Input/unit: payload mapping with bounded protocol values.
        Deadline: none; bounded in-memory work. Idempotency/effect: deterministic
        and side-effect free. Return: ``ConnectorWelcome``.
        Errors: missing, unknown, malformed, or contract-invalid fields.
        """

    def decode_heartbeat_payload(
        self,
        payload: Mapping[str, object],
    ) -> ConnectorHeartbeat:
        """Validate and decode a Connector Heartbeat payload.

        Input/unit: payload mapping with cursor values measured in sequences.
        Deadline: none; bounded in-memory work. Idempotency/effect: deterministic,
        side-effect free, and never a durable ACK. Return: ``ConnectorHeartbeat``.
        Errors: missing, unknown, malformed, or contract-invalid fields.
        """

    def heartbeat_payload(
        self,
        message: ConnectorHeartbeat,
    ) -> Mapping[str, object]:
        """Map a Connector Heartbeat to its payload object.

        Input/unit: one heartbeat with millisecond timestamp and sequence cursors.
        Deadline: none; bounded in-memory work. Idempotency/effect: deterministic,
        side-effect free, and never a durable ACK. Return: payload fields.
        Errors: invalid value or protocol-limit violations.
        """
