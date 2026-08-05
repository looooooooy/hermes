"""Ports required by the Connector Gateway session application."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from hermes_cloud.domain.connector_gateway import (
    ConnectorCommandDelivery,
    ConnectorHeartbeat,
    ConnectorHello,
    ConnectorIdentity,
    ConnectorObserverEvent,
    ConnectorObserverReceiptDelivery,
    ConnectorObserverSnapshot,
    ConnectorObserverSubscriptionDelivery,
    ConnectorResumePosition,
    ConnectorResumeResolution,
    ConnectorSessionCatalogEvent,
    ConnectorSessionCatalogReceiptDelivery,
    ConnectorSessionCatalogSnapshotPage,
)
from hermes_cloud.domain.contract_models import CloudEnvelope


class ConnectorAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> ConnectorIdentity:
        """Authenticate one opaque bearer token or raise without exposing it."""

    async def revalidate(self, identity: ConnectorIdentity) -> None:
        """Recheck authoritative lifecycle and credential state."""


class ConnectorResumeResolver(Protocol):
    async def resolve(
        self,
        identity: ConnectorIdentity,
        position: ConnectorResumePosition,
        *,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorResumeResolution:
        """Resolve a resume position from an injected durable authority."""


class ConnectorTransportCursorAuthority(ConnectorResumeResolver, Protocol):
    async def prepare_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        resume_decision: str,
        handshake_disposition: str,
        previous_connection_id: str | None,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        """Reserve expiring ownership without activating the connection."""

    async def confirm_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        """CAS a sent welcome into active cursor ownership."""

    async def abort_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        """Release only an exact unconfirmed handshake reservation."""

    async def commit_cursors(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        """Commit one terminal frame advance under exact ownership."""

    async def disconnect_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        """Mark only the exact live connection offline without clearing cursors."""


class ConnectorCommandRouter(Protocol):
    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        """Publish one authenticated live Connector binding."""

    async def connector_disconnected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        """Withdraw one exact Connector binding without affecting replacements."""

    async def wait_for_delivery(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorCommandDelivery | None:
        """Return the next durable command or wait until cancelled."""

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
        """Persist an exact dispatch reservation before its socket write."""

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
        """Refresh exact live presence and durable cursor projection."""

    async def accept_connector_response(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
    ) -> None:
        """Persist one receipt or result before advancing its inbound cursor."""


class ConnectorOwnerControlRouter(Protocol):
    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None: ...

    async def connector_disconnected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None: ...

    async def wait_for_control_request(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> Mapping[str, object] | None: ...

    async def control_request_effect_started(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        request_id: str,
    ) -> bool: ...


class ConnectorObserverIngress(Protocol):
    async def accept_snapshot(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorObserverSnapshot,
    ) -> None:
        """Commit one authenticated snapshot before the transport cursor advances."""

    async def accept_event(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorObserverEvent,
    ) -> None:
        """Commit one authenticated event before the transport cursor advances."""


class ConnectorSessionCatalogIngress(Protocol):
    async def accept_snapshot_page_and_advance(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorSessionCatalogSnapshotPage,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
    ) -> ConnectorSessionCatalogReceiptDelivery: ...

    async def accept_event_and_advance(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorSessionCatalogEvent,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
    ) -> ConnectorSessionCatalogReceiptDelivery: ...

    async def next_pending_receipt(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
    ) -> str | None: ...

    async def reserve_pending_receipt_and_advance(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        catalog_message_id: str,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
    ) -> ConnectorSessionCatalogReceiptDelivery: ...

    async def mark_receipt_sent(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        catalog_message_id: str,
        message_id: str,
        receipt_sequence: int,
    ) -> None: ...

    async def confirm_receipts_through_cursor(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        durable_next_inbound_sequence: int,
    ) -> int: ...


class ConnectorObserverReceiptRouter(Protocol):
    async def stage_and_reserve(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        receipt_type: str,
        payload: Mapping[str, object],
        sequence: int,
    ) -> ConnectorObserverReceiptDelivery: ...

    async def next_pending(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
    ) -> str | None: ...

    async def reserve_redelivery(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        sequence: int,
    ) -> ConnectorObserverReceiptDelivery: ...

    async def mark_sent(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        message_id: str,
        sequence: int,
    ) -> None: ...

    async def confirm_through_cursor(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        durable_next_inbound_sequence: int,
    ) -> int: ...


class ConnectorObserverSubscriptionRouter(Protocol):
    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None: ...

    async def connector_disconnected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None: ...

    async def wait_for_subscription_intent(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorObserverSubscriptionDelivery | None: ...

    async def reserve_subscription_intent(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        request_id: str,
        message_id: str,
        sequence: int,
        observer_contract: int,
        wire_message_type: str,
        wire_payload_digest: str,
    ) -> ConnectorObserverSubscriptionDelivery: ...

    async def connector_heartbeat(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None: ...

    async def accept_control_response(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        payload: Mapping[str, object],
    ) -> bool: ...


class ConnectorProtocolCodec(Protocol):
    def decode_connector_frame(self, raw: object) -> CloudEnvelope:
        """Decode one untrusted Cloud Envelope."""

    def decode_hello(self, payload: object) -> ConnectorHello:
        """Decode one authoritative Connector Hello payload."""

    def decode_heartbeat(self, payload: object) -> ConnectorHeartbeat:
        """Decode one authoritative Connector Heartbeat payload."""

    def decode_session_snapshot(self, payload: object) -> ConnectorObserverSnapshot:
        """Decode one authoritative Observer snapshot payload."""

    def decode_session_event(self, payload: object) -> ConnectorObserverEvent:
        """Decode one authoritative Observer event payload."""

    def decode_session_catalog_snapshot_page(
        self, payload: object
    ) -> ConnectorSessionCatalogSnapshotPage:
        """Decode one authoritative Session Catalog snapshot page."""

    def decode_session_catalog_event(
        self, payload: object
    ) -> ConnectorSessionCatalogEvent:
        """Decode one authoritative Session Catalog event."""

    def encode_connector_frame(self, envelope: CloudEnvelope) -> str:
        """Encode one validated Cloud Envelope as one text document."""


class ConnectorConnection(Protocol):
    @property
    def peer_disconnected(self) -> bool:
        """Return whether the peer already sent a disconnect event."""

    async def accept(self, *, timeout_seconds: float) -> None:
        """Accept one ASGI WebSocket within the deadline."""

    async def receive_text(self, *, timeout_seconds: float) -> str:
        """Receive exactly one text frame/document within the deadline."""

    async def send_text(self, text: str, *, timeout_seconds: float) -> None:
        """Send one text frame/document within the deadline."""

    async def close(
        self,
        *,
        code: int,
        reason: str,
        timeout_seconds: float,
    ) -> None:
        """Idempotently close the connection within the deadline."""
