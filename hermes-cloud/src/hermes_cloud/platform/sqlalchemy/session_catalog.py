"""Transactional Session Catalog v1 authority and read projection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorSessionCatalogEvent,
    ConnectorSessionCatalogReceiptDelivery,
    ConnectorSessionCatalogSnapshotPage,
    SessionCatalogEntry,
)
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.modules.projection.domain import CatalogSessionProjection
from hermes_cloud.platform.postgres.models import (
    DeviceLifecycleModel,
    SessionProjectionModel,
    WorkspaceMembershipModel,
)
from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (
    LockedConnectorTransportCursor,
    advance_locked_connector_transport_cursor,
    lock_active_connector_transport_epoch,
    lock_connector_transport_cursor,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogAuthorityModel,
    SessionCatalogEntryModel,
    SessionCatalogGenerationModel,
    SessionCatalogInboxModel,
    SessionCatalogSnapshotPageModel,
)

_ANCHOR_RETENTION = timedelta(days=3650)
_CATALOG_INBOX_RETENTION = timedelta(days=7)
_CATALOG_STAGING_TTL = timedelta(minutes=10)
_CATALOG_PENDING_RECEIPT_CAPACITY = 1_024


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...

    def __call__(self) -> AbstractContextManager[Session]: ...


class SessionCatalogUnauthorized(PermissionError):
    """Authenticated Connector has no active Agent binding."""


@dataclass(frozen=True, slots=True)
class SessionCatalogCleanupResult:
    inbox_deleted: int
    snapshot_pages_deleted: int
    authorities_reset: int


class SqlAlchemySessionCatalogRetentionCleaner:
    """Delete expired replay/staging rows in bounded ORM batches."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch_size: int = 256,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("catalog cleanup batch size must be positive")
        self._session_factory = session_factory
        self._now = now
        self._batch_size = batch_size

    async def cleanup_once(self) -> SessionCatalogCleanupResult:
        return await asyncio.to_thread(self._cleanup_once)

    def _cleanup_once(self) -> SessionCatalogCleanupResult:
        now = self._now()
        with self._session_factory.begin() as session:
            authorities = session.scalars(
                select(SessionCatalogAuthorityModel)
                .where(
                    SessionCatalogAuthorityModel.staging_deadline.is_not(None),
                    SessionCatalogAuthorityModel.staging_deadline <= now,
                )
                .order_by(
                    SessionCatalogAuthorityModel.tenant_id,
                    SessionCatalogAuthorityModel.agent_id,
                    SessionCatalogAuthorityModel.profile,
                )
                .limit(self._batch_size)
                .with_for_update()
            ).all()
            for authority in authorities:
                authority.staging_snapshot_id = None
                authority.staging_runtime_generation = None
                authority.staging_catalog_revision = None
                authority.staging_deadline = None
                authority.expected_page_index = 0
                authority.require_full_snapshot = True
                authority.updated_at = now

            pages = session.scalars(
                select(SessionCatalogSnapshotPageModel)
                .where(
                    SessionCatalogSnapshotPageModel.created_at
                    <= now - _CATALOG_STAGING_TTL
                )
                .order_by(
                    SessionCatalogSnapshotPageModel.created_at,
                    SessionCatalogSnapshotPageModel.tenant_id,
                    SessionCatalogSnapshotPageModel.agent_id,
                    SessionCatalogSnapshotPageModel.profile,
                    SessionCatalogSnapshotPageModel.snapshot_id,
                    SessionCatalogSnapshotPageModel.page_index,
                )
                .limit(self._batch_size)
                .with_for_update()
            ).all()
            for page in pages:
                session.delete(page)

            inbox_rows = session.scalars(
                select(SessionCatalogInboxModel)
                .where(
                    SessionCatalogInboxModel.retention_until <= now,
                    or_(
                        SessionCatalogInboxModel.receipt_state.is_(None),
                        SessionCatalogInboxModel.receipt_state == "settled",
                        SessionCatalogInboxModel.receipt_state == "retired",
                    ),
                )
                .order_by(
                    SessionCatalogInboxModel.retention_until,
                    SessionCatalogInboxModel.tenant_id,
                    SessionCatalogInboxModel.message_id,
                )
                .limit(self._batch_size)
                .with_for_update()
            ).all()
            for inbox in inbox_rows:
                session.delete(inbox)
        return SessionCatalogCleanupResult(
            inbox_deleted=len(inbox_rows),
            snapshot_pages_deleted=len(pages),
            authorities_reset=len(authorities),
        )


class SessionCatalogRejected(RuntimeError):
    """A semantic catalog position requires a contract NACK."""

    def __init__(
        self,
        reason: str,
        *,
        snapshot_id: str | None = None,
        expected_page_index: int | None = None,
        expected_catalog_sequence: int | None = None,
    ) -> None:
        if reason not in {
            "page_gap",
            "event_gap",
            "runtime_mismatch",
            "stale_writer",
            "contract_mismatch",
            "revision_conflict",
        }:
            raise ValueError("catalog rejection reason is invalid")
        super().__init__("session catalog message rejected")
        self.reason = reason
        self.snapshot_id = snapshot_id
        self.expected_page_index = expected_page_index
        self.expected_catalog_sequence = expected_catalog_sequence


def _identity_ids(identity: ConnectorIdentity) -> tuple[UUID, UUID, UUID]:
    if identity.agent_id is None or "session.observe" not in identity.scopes:
        raise SessionCatalogUnauthorized("catalog identity requires scoped agent")
    try:
        return (
            UUID(identity.tenant_id),
            UUID(identity.device_id),
            UUID(identity.agent_id),
        )
    except ValueError as error:
        raise SessionCatalogUnauthorized("catalog identity is not tenant scoped") from error


def _active_workspace(
    session: Session,
    *,
    tenant_id: UUID,
    device_id: UUID,
    agent_id: UUID,
) -> UUID:
    lifecycle = session.scalar(
        select(DeviceLifecycleModel).where(
            DeviceLifecycleModel.tenant_id == tenant_id,
            DeviceLifecycleModel.device_id == device_id,
            DeviceLifecycleModel.agent_id == agent_id,
            DeviceLifecycleModel.state == "active",
        )
    )
    if lifecycle is None:
        raise SessionCatalogUnauthorized("catalog identity has no active lifecycle")
    return UUID(str(lifecycle.workspace_id))


def _entry_record(entry: SessionCatalogEntry) -> dict[str, object]:
    return {
        "session_key": entry.session_key,
        "surface": entry.surface,
        "authority_revision": entry.authority_revision,
        "available_actions": list(entry.available_actions),
    }


def _receipt(row: SessionCatalogInboxModel) -> ConnectorSessionCatalogReceiptDelivery:
    if (
        row.receipt_type is None
        or row.receipt_payload is None
        or row.dispatch_message_id is None
        or row.dispatch_sequence is None
        or row.dispatch_connection_id is None
    ):
        raise RuntimeError("catalog receipt dispatch is incomplete")
    return ConnectorSessionCatalogReceiptDelivery(
        catalog_message_id=str(row.message_id),
        message_id=str(row.dispatch_message_id),
        message_type=row.receipt_type,
        sequence=int(row.dispatch_sequence),
        sent_at=(
            row.updated_at.replace(tzinfo=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        ),
        payload=dict(row.receipt_payload),
    )


class SqlAlchemySessionCatalogIngress:
    """Apply catalog messages and persist their exact business receipts atomically."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], UUID] = uuid4,
        ownership_lease_seconds: float = 90.0,
    ) -> None:
        if ownership_lease_seconds <= 0:
            raise ValueError("catalog ownership lease must be positive")
        self._session_factory = session_factory
        self._now = now
        self._id_factory = id_factory
        self._ownership_lease = timedelta(seconds=ownership_lease_seconds)

    async def accept_snapshot_page(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorSessionCatalogSnapshotPage,
    ) -> ConnectorSessionCatalogReceiptDelivery | None:
        return await asyncio.to_thread(
            self._accept_snapshot_page,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
            payload,
            None,
            None,
        )

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
    ) -> ConnectorSessionCatalogReceiptDelivery | None:
        return await asyncio.to_thread(
            self._accept_snapshot_page,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
            payload,
            expected_next_connector_sequence,
            expected_next_cloud_sequence,
        )

    async def accept_event(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorSessionCatalogEvent,
    ) -> ConnectorSessionCatalogReceiptDelivery:
        return await asyncio.to_thread(
            self._accept_event,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
            payload,
            None,
            None,
        )

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
    ) -> ConnectorSessionCatalogReceiptDelivery:
        return await asyncio.to_thread(
            self._accept_event,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
            payload,
            expected_next_connector_sequence,
            expected_next_cloud_sequence,
        )

    async def next_pending_receipt(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
    ) -> str | None:
        return await asyncio.to_thread(
            self._next_pending_receipt,
            identity,
            connection_id,
        )

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
    ) -> ConnectorSessionCatalogReceiptDelivery:
        return await asyncio.to_thread(
            self._reserve_pending_receipt_and_advance,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            catalog_message_id,
            expected_next_connector_sequence,
            expected_next_cloud_sequence,
        )

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
    ) -> None:
        await asyncio.to_thread(
            self._mark_receipt_sent,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            catalog_message_id,
            message_id,
            receipt_sequence,
        )

    async def confirm_receipts_through_cursor(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        durable_next_inbound_sequence: int,
    ) -> int:
        return await asyncio.to_thread(
            self._confirm_receipts_through_cursor,
            identity,
            connection_id,
            durable_next_inbound_sequence,
        )

    def _next_pending_receipt(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
    ) -> str | None:
        tenant_id, device_id, _agent_id = _identity_ids(identity)
        connection = UUID(connection_id)
        now = self._now()
        with self._session_factory.begin() as session:
            locked = lock_active_connector_transport_epoch(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=None,
                runtime_generation=None,
                now=now,
            )
            connector_instance_id: UUID = locked.connector_instance_id
            runtime_generation: str = locked.runtime_generation
            message_id = session.scalar(
                select(SessionCatalogInboxModel.message_id)
                .where(
                    SessionCatalogInboxModel.tenant_id == tenant_id,
                    SessionCatalogInboxModel.device_id == device_id,
                    SessionCatalogInboxModel.connector_instance_id
                    == connector_instance_id,
                    SessionCatalogInboxModel.runtime_generation
                    == runtime_generation,
                    SessionCatalogInboxModel.receipt_state == "pending",
                    or_(
                        SessionCatalogInboxModel.dispatch_connection_id.is_(None),
                        SessionCatalogInboxModel.dispatch_connection_id != connection,
                    ),
                )
                .order_by(
                    SessionCatalogInboxModel.updated_at,
                    SessionCatalogInboxModel.message_id,
                )
                .limit(1)
            )
            return None if message_id is None else str(message_id)

    def _reserve_pending_receipt_and_advance(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        catalog_message_id: str,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
    ) -> ConnectorSessionCatalogReceiptDelivery:
        tenant_id, device_id, _agent_id = _identity_ids(identity)
        now = self._now()
        instance_id = UUID(connector_instance_id)
        message_id = UUID(catalog_message_id)
        with self._session_factory.begin() as session:
            locked_transport = self._lock_transport(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                expected_next_connector_sequence=expected_next_connector_sequence,
                expected_next_cloud_sequence=expected_next_cloud_sequence,
                now=now,
            )
            if locked_transport is None:
                raise AssertionError("pending receipt reservation requires transport")
            inbox = session.get(
                SessionCatalogInboxModel,
                (tenant_id, message_id),
                with_for_update=True,
            )
            if (
                inbox is None
                or inbox.device_id != device_id
                or inbox.connector_instance_id != instance_id
                or inbox.runtime_generation != runtime_generation
                or inbox.receipt_state != "pending"
                or inbox.dispatch_connection_id == UUID(connection_id)
            ):
                raise RuntimeError("catalog receipt pending delivery changed")
            inbox.dispatch_connection_id = UUID(connection_id)
            inbox.dispatch_message_id = self._id_factory()
            inbox.dispatch_sequence = expected_next_cloud_sequence
            inbox.dispatch_attempts += 1
            inbox.updated_at = now
            inbox.receipt_sent_at = None
            advance_locked_connector_transport_cursor(
                session,
                locked=locked_transport,
                next_connector_sequence=expected_next_connector_sequence,
                next_cloud_sequence=expected_next_cloud_sequence + 1,
                now=now,
                ownership_lease=self._ownership_lease,
            )
            return _receipt(inbox)

    def _mark_receipt_sent(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        catalog_message_id: str,
        message_id: str,
        receipt_sequence: int,
    ) -> None:
        tenant_id, device_id, _agent_id = _identity_ids(identity)
        instance_id = UUID(connector_instance_id)
        catalog_id = UUID(catalog_message_id)
        dispatch_id = UUID(message_id)
        if receipt_sequence < 0:
            raise ValueError("catalog receipt sequence is invalid")
        now = self._now()
        with self._session_factory.begin() as session:
            try:
                lock_active_connector_transport_epoch(
                    session,
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    now=now,
                )
            except RuntimeError:
                raise RuntimeError(
                    "catalog receipt dispatch ownership changed"
                ) from None
            inbox = session.get(
                SessionCatalogInboxModel,
                (tenant_id, catalog_id),
                with_for_update=True,
            )
            if (
                inbox is None
                or inbox.device_id != device_id
                or inbox.connector_instance_id != instance_id
                or inbox.runtime_generation != runtime_generation
                or inbox.receipt_state != "pending"
                or inbox.dispatch_connection_id != UUID(connection_id)
                or inbox.dispatch_message_id != dispatch_id
                or inbox.dispatch_sequence != receipt_sequence
                or inbox.receipt_type is None
                or inbox.receipt_payload is None
            ):
                raise RuntimeError("catalog receipt dispatch ownership changed")
            inbox.receipt_sent_at = now
            inbox.updated_at = now

    def _confirm_receipts_through_cursor(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        durable_next_inbound_sequence: int,
    ) -> int:
        tenant_id, device_id, _agent_id = _identity_ids(identity)
        if durable_next_inbound_sequence < 0:
            raise ValueError("catalog durable receipt cursor is invalid")
        connection = UUID(connection_id)
        now = self._now()
        with self._session_factory.begin() as session:
            try:
                locked = lock_active_connector_transport_epoch(
                    session,
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=None,
                    runtime_generation=None,
                    now=now,
                )
            except RuntimeError:
                return 0
            connector_instance_id: UUID = locked.connector_instance_id
            runtime_generation: str = locked.runtime_generation
            rows = session.scalars(
                select(SessionCatalogInboxModel)
                .where(
                    SessionCatalogInboxModel.tenant_id == tenant_id,
                    SessionCatalogInboxModel.device_id == device_id,
                    SessionCatalogInboxModel.connector_instance_id
                    == connector_instance_id,
                    SessionCatalogInboxModel.runtime_generation
                    == runtime_generation,
                    SessionCatalogInboxModel.receipt_state == "pending",
                    SessionCatalogInboxModel.dispatch_connection_id == connection,
                    SessionCatalogInboxModel.dispatch_sequence.is_not(None),
                    SessionCatalogInboxModel.dispatch_sequence
                    < durable_next_inbound_sequence,
                    SessionCatalogInboxModel.receipt_sent_at.is_not(None),
                )
                .order_by(
                    SessionCatalogInboxModel.dispatch_sequence,
                    SessionCatalogInboxModel.message_id,
                )
                .limit(_CATALOG_PENDING_RECEIPT_CAPACITY)
                .with_for_update()
            ).all()
            for inbox in rows:
                inbox.receipt_state = "settled"
                inbox.receipt_settled_at = now
                inbox.updated_at = now
            return len(rows)

    def _accept_snapshot_page(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorSessionCatalogSnapshotPage,
        expected_next_connector_sequence: int | None,
        expected_next_cloud_sequence: int | None,
    ) -> ConnectorSessionCatalogReceiptDelivery | None:
        tenant_id, device_id, agent_id = _identity_ids(identity)
        digest = canonical_payload_digest(envelope.payload)
        now = self._now()
        with self._session_factory.begin() as session:
            locked_transport = self._lock_transport(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                expected_next_connector_sequence=expected_next_connector_sequence,
                expected_next_cloud_sequence=expected_next_cloud_sequence,
                now=now,
            )
            try:
                workspace_id = _active_workspace(
                    session,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    agent_id=agent_id,
                )
            except SessionCatalogUnauthorized:
                stale = self._stale_writer_receipt(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=payload.profile,
                    connector_instance_id=UUID(connector_instance_id),
                    runtime_generation=runtime_generation,
                    envelope=envelope,
                    digest=digest,
                    dispatch_connection_id=UUID(connection_id),
                    dispatch_sequence=(
                        locked_transport.expected_next_cloud_sequence
                        if locked_transport is not None
                        else 0
                    ),
                    now=now,
                )
                if stale is not None:
                    self._advance_transport(
                        session,
                        locked_transport,
                        receipt_present=True,
                        now=now,
                    )
                    return stale
                raise
            try:
                duplicate = self._existing_receipt(
                    session,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    connector_instance_id=UUID(connector_instance_id),
                    runtime_generation=runtime_generation,
                    envelope=envelope,
                    digest=digest,
                )
            except SessionCatalogRejected as rejection:
                self._mark_recovery_required(
                    session,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=payload.profile,
                    rejection=rejection,
                    now=now,
                )
                receipt_payload = self._nack_payload(
                    envelope=envelope,
                    profile=payload.profile,
                    runtime_generation=payload.runtime_generation,
                    digest=digest,
                    rejection=rejection,
                )
                self._advance_transport(
                    session,
                    locked_transport,
                    receipt_present=True,
                    now=now,
                )
                return self._stage_conflict_receipt(
                    session,
                    tenant_id=tenant_id,
                    message_id=UUID(envelope.message_id),
                    receipt_payload=receipt_payload,
                    connection_id=UUID(connection_id),
                    sequence=(
                        locked_transport.expected_next_cloud_sequence
                        if locked_transport is not None
                        else 0
                    ),
                    now=now,
                )
            if duplicate is not False:
                self._advance_transport(
                    session,
                    locked_transport,
                    receipt_present=duplicate is not None,
                    now=now,
                )
                return duplicate
            try:
                with session.begin_nested():
                    if payload.runtime_generation != runtime_generation:
                        raise SessionCatalogRejected("runtime_mismatch")
                    terminal = self._apply_snapshot_page(
                        session,
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        device_id=device_id,
                        payload=payload,
                        digest=digest,
                        now=now,
                    )
                    receipt_type = None
                    receipt_payload = None
                    if terminal:
                        receipt_type = "session.catalog.ack"
                        receipt_payload = {
                            "profile": payload.profile,
                            "runtime_generation": payload.runtime_generation,
                            "acked_message_id": envelope.message_id,
                            "acked_payload_digest": digest,
                            "acked_connector_sequence": envelope.sequence,
                            "ack_kind": "snapshot_committed",
                            "snapshot_id": payload.snapshot_id,
                            "catalog_revision": payload.catalog_revision,
                            "page_index": payload.page_index,
                            "is_last": True,
                        }
            except SessionCatalogRejected as rejection:
                self._mark_recovery_required(
                    session,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=payload.profile,
                    rejection=rejection,
                    now=now,
                )
                receipt_type = "session.catalog.nack"
                receipt_payload = self._nack_payload(
                    envelope=envelope,
                    profile=payload.profile,
                    runtime_generation=payload.runtime_generation,
                    digest=digest,
                    rejection=rejection,
                )
            inbox = self._store_inbox(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                connector_instance_id=UUID(connector_instance_id),
                runtime_generation=runtime_generation,
                envelope=envelope,
                digest=digest,
                receipt_type=receipt_type,
                receipt_payload=receipt_payload,
                dispatch_connection_id=(
                    UUID(connection_id) if receipt_type is not None else None
                ),
                dispatch_sequence=(
                    locked_transport.expected_next_cloud_sequence
                    if receipt_type is not None and locked_transport is not None
                    else (0 if receipt_type is not None else None)
                ),
                now=now,
            )
            self._advance_transport(
                session,
                locked_transport,
                receipt_present=receipt_type is not None,
                now=now,
            )
            return (
                None
                if receipt_type is None or receipt_payload is None
                else _receipt(inbox)
            )

    def _accept_event(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorSessionCatalogEvent,
        expected_next_connector_sequence: int | None,
        expected_next_cloud_sequence: int | None,
    ) -> ConnectorSessionCatalogReceiptDelivery:
        tenant_id, device_id, agent_id = _identity_ids(identity)
        digest = canonical_payload_digest(envelope.payload)
        now = self._now()
        with self._session_factory.begin() as session:
            locked_transport = self._lock_transport(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                expected_next_connector_sequence=expected_next_connector_sequence,
                expected_next_cloud_sequence=expected_next_cloud_sequence,
                now=now,
            )
            try:
                workspace_id = _active_workspace(
                    session,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    agent_id=agent_id,
                )
            except SessionCatalogUnauthorized:
                stale = self._stale_writer_receipt(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=payload.profile,
                    connector_instance_id=UUID(connector_instance_id),
                    runtime_generation=runtime_generation,
                    envelope=envelope,
                    digest=digest,
                    dispatch_connection_id=UUID(connection_id),
                    dispatch_sequence=(
                        locked_transport.expected_next_cloud_sequence
                        if locked_transport is not None
                        else 0
                    ),
                    now=now,
                )
                if stale is not None:
                    self._advance_transport(
                        session,
                        locked_transport,
                        receipt_present=True,
                        now=now,
                    )
                    return stale
                raise
            try:
                duplicate = self._existing_receipt(
                    session,
                    tenant_id=tenant_id,
                    device_id=device_id,
                    connector_instance_id=UUID(connector_instance_id),
                    runtime_generation=runtime_generation,
                    envelope=envelope,
                    digest=digest,
                )
            except SessionCatalogRejected as rejection:
                self._mark_recovery_required(
                    session,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=payload.profile,
                    rejection=rejection,
                    now=now,
                )
                receipt_payload = self._nack_payload(
                    envelope=envelope,
                    profile=payload.profile,
                    runtime_generation=payload.runtime_generation,
                    digest=digest,
                    rejection=rejection,
                )
                self._advance_transport(
                    session,
                    locked_transport,
                    receipt_present=True,
                    now=now,
                )
                return self._stage_conflict_receipt(
                    session,
                    tenant_id=tenant_id,
                    message_id=UUID(envelope.message_id),
                    receipt_payload=receipt_payload,
                    connection_id=UUID(connection_id),
                    sequence=(
                        locked_transport.expected_next_cloud_sequence
                        if locked_transport is not None
                        else 0
                    ),
                    now=now,
                )
            if duplicate is not False:
                if duplicate is None:
                    rejection = SessionCatalogRejected("contract_mismatch")
                    receipt = self._stage_conflict_receipt(
                        session,
                        tenant_id=tenant_id,
                        message_id=UUID(envelope.message_id),
                        receipt_payload=self._nack_payload(
                            envelope=envelope,
                            profile=payload.profile,
                            runtime_generation=payload.runtime_generation,
                            digest=digest,
                            rejection=rejection,
                        ),
                        connection_id=UUID(connection_id),
                        sequence=(
                            locked_transport.expected_next_cloud_sequence
                            if locked_transport is not None
                            else 0
                        ),
                        now=now,
                    )
                    self._advance_transport(
                        session,
                        locked_transport,
                        receipt_present=True,
                        now=now,
                    )
                    return receipt
                self._advance_transport(
                    session,
                    locked_transport,
                    receipt_present=True,
                    now=now,
                )
                return duplicate
            try:
                with session.begin_nested():
                    if payload.runtime_generation != runtime_generation:
                        raise SessionCatalogRejected("runtime_mismatch")
                    self._apply_event(
                        session,
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        device_id=device_id,
                        payload=payload,
                        now=now,
                    )
                    receipt_type = "session.catalog.ack"
                    receipt_payload = {
                        "profile": payload.profile,
                        "runtime_generation": payload.runtime_generation,
                        "acked_message_id": envelope.message_id,
                        "acked_payload_digest": digest,
                        "acked_connector_sequence": envelope.sequence,
                        "ack_kind": "event_applied",
                        "catalog_sequence": payload.catalog_sequence,
                    }
            except SessionCatalogRejected as rejection:
                self._mark_recovery_required(
                    session,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=payload.profile,
                    rejection=rejection,
                    now=now,
                )
                receipt_type = "session.catalog.nack"
                receipt_payload = self._nack_payload(
                    envelope=envelope,
                    profile=payload.profile,
                    runtime_generation=payload.runtime_generation,
                    digest=digest,
                    rejection=rejection,
                )
            inbox = self._store_inbox(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                connector_instance_id=UUID(connector_instance_id),
                runtime_generation=runtime_generation,
                envelope=envelope,
                digest=digest,
                receipt_type=receipt_type,
                receipt_payload=receipt_payload,
                dispatch_connection_id=UUID(connection_id),
                dispatch_sequence=(
                    locked_transport.expected_next_cloud_sequence
                    if locked_transport is not None
                    else 0
                ),
                now=now,
            )
            self._advance_transport(
                session,
                locked_transport,
                receipt_present=True,
                now=now,
            )
            return _receipt(inbox)

    def _lock_transport(
        self,
        session: Session,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        expected_next_connector_sequence: int | None,
        expected_next_cloud_sequence: int | None,
        now: datetime,
    ) -> LockedConnectorTransportCursor | None:
        if (
            expected_next_connector_sequence is None
            and expected_next_cloud_sequence is None
        ):
            return None
        if (
            expected_next_connector_sequence is None
            or expected_next_cloud_sequence is None
        ):
            raise ValueError("catalog transport cursor expectation is incomplete")
        return lock_connector_transport_cursor(
            session,
            identity=identity,
            connection_id=connection_id,
            connector_instance_id=connector_instance_id,
            runtime_generation=runtime_generation,
            expected_next_connector_sequence=expected_next_connector_sequence,
            expected_next_cloud_sequence=expected_next_cloud_sequence,
            now=now,
        )

    def _advance_transport(
        self,
        session: Session,
        locked: LockedConnectorTransportCursor | None,
        *,
        receipt_present: bool,
        now: datetime,
    ) -> None:
        if locked is None:
            return
        advance_locked_connector_transport_cursor(
            session,
            locked=locked,
            next_connector_sequence=(locked.expected_next_connector_sequence + 1),
            next_cloud_sequence=(
                locked.expected_next_cloud_sequence + int(receipt_present)
            ),
            now=now,
            ownership_lease=self._ownership_lease,
        )

    def _stale_writer_receipt(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        profile: str,
        connector_instance_id: UUID,
        runtime_generation: str,
        envelope: CloudEnvelope,
        digest: str,
        dispatch_connection_id: UUID,
        dispatch_sequence: int,
        now: datetime,
    ) -> ConnectorSessionCatalogReceiptDelivery | None:
        authority = session.get(
            SessionCatalogAuthorityModel,
            (tenant_id, agent_id, profile),
            with_for_update=True,
        )
        if authority is None or authority.writer_id == device_id:
            return None
        rejection = SessionCatalogRejected("stale_writer")
        receipt_payload = self._nack_payload(
            envelope=envelope,
            profile=profile,
            runtime_generation=runtime_generation,
            digest=digest,
            rejection=rejection,
        )
        inbox = self._store_inbox(
            session,
            tenant_id=tenant_id,
            workspace_id=authority.workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            connector_instance_id=connector_instance_id,
            runtime_generation=runtime_generation,
            envelope=envelope,
            digest=digest,
            receipt_type="session.catalog.nack",
            receipt_payload=receipt_payload,
            dispatch_connection_id=dispatch_connection_id,
            dispatch_sequence=dispatch_sequence,
            now=now,
        )
        return _receipt(inbox)

    def _existing_receipt(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        device_id: UUID,
        connector_instance_id: UUID,
        runtime_generation: str,
        envelope: CloudEnvelope,
        digest: str,
    ) -> ConnectorSessionCatalogReceiptDelivery | None | bool:
        existing = session.get(
            SessionCatalogInboxModel,
            (tenant_id, UUID(envelope.message_id)),
        )
        if existing is None:
            collision = session.scalar(
                select(SessionCatalogInboxModel).where(
                    SessionCatalogInboxModel.tenant_id == tenant_id,
                    SessionCatalogInboxModel.device_id == device_id,
                    SessionCatalogInboxModel.connector_instance_id
                    == connector_instance_id,
                    SessionCatalogInboxModel.runtime_generation
                    == runtime_generation,
                    SessionCatalogInboxModel.connector_sequence == envelope.sequence,
                )
            )
            if collision is not None:
                raise SessionCatalogRejected("contract_mismatch")
            return False
        if (
            existing.device_id != device_id
            or existing.connector_instance_id != connector_instance_id
            or existing.runtime_generation != runtime_generation
            or existing.connector_sequence != envelope.sequence
            or existing.message_type != envelope.message_type
            or existing.payload_digest != digest
        ):
            raise SessionCatalogRejected("contract_mismatch")
        if existing.receipt_type is None or existing.receipt_payload is None:
            return None
        return _receipt(existing)

    def _store_inbox(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        connector_instance_id: UUID,
        runtime_generation: str,
        envelope: CloudEnvelope,
        digest: str,
        receipt_type: str | None,
        receipt_payload: dict[str, object] | None,
        dispatch_connection_id: UUID | None,
        dispatch_sequence: int | None,
        now: datetime,
    ) -> SessionCatalogInboxModel:
        if receipt_type is not None:
            pending = session.scalars(
                select(SessionCatalogInboxModel.message_id)
                .where(
                    SessionCatalogInboxModel.tenant_id == tenant_id,
                    SessionCatalogInboxModel.device_id == device_id,
                    SessionCatalogInboxModel.receipt_state == "pending",
                )
                .limit(_CATALOG_PENDING_RECEIPT_CAPACITY)
            ).all()
            if len(pending) >= _CATALOG_PENDING_RECEIPT_CAPACITY:
                raise RuntimeError("catalog receipt pending capacity reached")
        has_receipt = receipt_type is not None and receipt_payload is not None
        row = SessionCatalogInboxModel(
            tenant_id=tenant_id,
            message_id=UUID(envelope.message_id),
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            connector_instance_id=connector_instance_id,
            runtime_generation=runtime_generation,
            connector_sequence=envelope.sequence,
            message_type=envelope.message_type,
            payload_digest=digest,
            receipt_type=receipt_type,
            receipt_payload=receipt_payload,
            receipt_state="pending" if has_receipt else None,
            dispatch_connection_id=(
                dispatch_connection_id if has_receipt else None
            ),
            dispatch_message_id=self._id_factory() if has_receipt else None,
            dispatch_sequence=dispatch_sequence if has_receipt else None,
            dispatch_attempts=1 if has_receipt else 0,
            received_at=now,
            updated_at=now,
            receipt_sent_at=None,
            receipt_settled_at=None,
            receipt_retired_at=None,
            receipt_retirement_reason=None,
            retention_until=now + _CATALOG_INBOX_RETENTION,
        )
        session.add(row)
        session.flush()
        return row

    def _stage_conflict_receipt(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        message_id: UUID,
        receipt_payload: dict[str, object],
        connection_id: UUID,
        sequence: int,
        now: datetime,
    ) -> ConnectorSessionCatalogReceiptDelivery:
        inbox = session.get(
            SessionCatalogInboxModel,
            (tenant_id, message_id),
            with_for_update=True,
        )
        if inbox is None:
            raise RuntimeError("catalog sequence collision requires recovery")
        inbox.receipt_type = "session.catalog.nack"
        inbox.receipt_payload = receipt_payload
        inbox.receipt_state = "pending"
        inbox.dispatch_connection_id = connection_id
        inbox.dispatch_message_id = self._id_factory()
        inbox.dispatch_sequence = sequence
        inbox.dispatch_attempts += 1
        inbox.updated_at = now
        inbox.receipt_sent_at = None
        inbox.receipt_settled_at = None
        inbox.receipt_retired_at = None
        inbox.receipt_retirement_reason = None
        return _receipt(inbox)

    def _authority(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        profile: str,
        now: datetime,
        create: bool,
        allow_takeover: bool = False,
    ) -> SessionCatalogAuthorityModel | None:
        row = session.get(
            SessionCatalogAuthorityModel,
            (tenant_id, agent_id, profile),
            with_for_update=True,
        )
        if row is None and create:
            row = SessionCatalogAuthorityModel(
                tenant_id=tenant_id,
                agent_id=agent_id,
                profile=profile,
                workspace_id=workspace_id,
                writer_id=device_id,
                writer_fence=1,
                runtime_generation=None,
                catalog_revision=0,
                catalog_sequence=0,
                staging_snapshot_id=None,
                staging_runtime_generation=None,
                staging_catalog_revision=None,
                staging_deadline=None,
                require_full_snapshot=False,
                expected_page_index=0,
                updated_at=now,
            )
            session.add(row)
            session.flush()
        if row is not None and row.writer_id != device_id:
            if not allow_takeover:
                raise SessionCatalogRejected("stale_writer")
            previous_writer_active = session.scalar(
                select(DeviceLifecycleModel.device_id).where(
                    DeviceLifecycleModel.tenant_id == tenant_id,
                    DeviceLifecycleModel.device_id == row.writer_id,
                    DeviceLifecycleModel.agent_id == agent_id,
                    DeviceLifecycleModel.workspace_id == workspace_id,
                    DeviceLifecycleModel.state == "active",
                )
            )
            if previous_writer_active is not None:
                raise SessionCatalogRejected("stale_writer")
            session.execute(
                delete(SessionCatalogSnapshotPageModel).where(
                    SessionCatalogSnapshotPageModel.tenant_id == tenant_id,
                    SessionCatalogSnapshotPageModel.agent_id == agent_id,
                    SessionCatalogSnapshotPageModel.profile == profile,
                )
            )
            row.writer_id = device_id
            row.writer_fence += 1
            row.workspace_id = workspace_id
            row.staging_snapshot_id = None
            row.staging_runtime_generation = None
            row.staging_catalog_revision = None
            row.staging_deadline = None
            row.expected_page_index = 0
            row.require_full_snapshot = True
            row.updated_at = now
        return row

    def _mark_recovery_required(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        profile: str,
        rejection: SessionCatalogRejected,
        now: datetime,
    ) -> None:
        if rejection.reason not in {
            "page_gap",
            "event_gap",
            "contract_mismatch",
            "revision_conflict",
        }:
            return
        authority = self._authority(
            session,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            profile=profile,
            now=now,
            create=True,
        )
        if authority is None:
            raise AssertionError("catalog recovery authority was not created")
        session.execute(
            delete(SessionCatalogSnapshotPageModel).where(
                SessionCatalogSnapshotPageModel.tenant_id == tenant_id,
                SessionCatalogSnapshotPageModel.agent_id == agent_id,
                SessionCatalogSnapshotPageModel.profile == profile,
            )
        )
        authority.staging_snapshot_id = None
        authority.staging_runtime_generation = None
        authority.staging_catalog_revision = None
        authority.staging_deadline = None
        authority.expected_page_index = 0
        authority.require_full_snapshot = True
        authority.updated_at = now

    def _apply_snapshot_page(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        payload: ConnectorSessionCatalogSnapshotPage,
        digest: str,
        now: datetime,
    ) -> bool:
        authority = self._authority(
            session,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            profile=payload.profile,
            now=now,
            create=payload.page_index == 0,
            allow_takeover=payload.page_index == 0,
        )
        if authority is None:
            raise SessionCatalogRejected(
                "page_gap",
                snapshot_id=payload.snapshot_id,
                expected_page_index=0,
            )
        old_generation = session.get(
            SessionCatalogGenerationModel,
            (tenant_id, agent_id, payload.profile, payload.runtime_generation),
        )
        if old_generation is not None and not old_generation.active:
            raise SessionCatalogRejected("runtime_mismatch")
        snapshot_id = UUID(payload.snapshot_id)
        if (
            payload.page_index == 0
            and authority.require_full_snapshot
            and authority.staging_snapshot_id is not None
        ):
            session.execute(
                delete(SessionCatalogSnapshotPageModel).where(
                    SessionCatalogSnapshotPageModel.tenant_id == tenant_id,
                    SessionCatalogSnapshotPageModel.agent_id == agent_id,
                    SessionCatalogSnapshotPageModel.profile == payload.profile,
                )
            )
            authority.staging_snapshot_id = None
            authority.staging_runtime_generation = None
            authority.staging_catalog_revision = None
            authority.staging_deadline = None
            authority.expected_page_index = 0
        if payload.page_index == 0 and authority.staging_snapshot_id is None:
            if (
                authority.runtime_generation == payload.runtime_generation
                and payload.catalog_revision < authority.catalog_revision
            ):
                raise SessionCatalogRejected(
                    "revision_conflict",
                    snapshot_id=payload.snapshot_id,
                    expected_page_index=0,
                )
            authority.staging_snapshot_id = snapshot_id
            authority.staging_runtime_generation = payload.runtime_generation
            authority.staging_catalog_revision = payload.catalog_revision
            authority.staging_deadline = now + _CATALOG_STAGING_TTL
            authority.expected_page_index = 0
        if (
            authority.staging_snapshot_id != snapshot_id
            or authority.staging_runtime_generation != payload.runtime_generation
            or authority.staging_catalog_revision != payload.catalog_revision
        ):
            raise SessionCatalogRejected(
                "revision_conflict",
                snapshot_id=str(authority.staging_snapshot_id or snapshot_id),
                expected_page_index=authority.expected_page_index,
            )
        if payload.page_index != authority.expected_page_index:
            raise SessionCatalogRejected(
                "page_gap",
                snapshot_id=payload.snapshot_id,
                expected_page_index=authority.expected_page_index,
            )
        session.add(
            SessionCatalogSnapshotPageModel(
                tenant_id=tenant_id,
                agent_id=agent_id,
                profile=payload.profile,
                snapshot_id=snapshot_id,
                page_index=payload.page_index,
                runtime_generation=payload.runtime_generation,
                catalog_revision=payload.catalog_revision,
                is_last=payload.is_last,
                sessions=[_entry_record(item) for item in payload.sessions],
                payload_digest=digest,
                created_at=now,
            )
        )
        authority.expected_page_index += 1
        authority.updated_at = now
        if not payload.is_last:
            return False
        pages = session.scalars(
            select(SessionCatalogSnapshotPageModel)
            .where(
                SessionCatalogSnapshotPageModel.tenant_id == tenant_id,
                SessionCatalogSnapshotPageModel.agent_id == agent_id,
                SessionCatalogSnapshotPageModel.profile == payload.profile,
                SessionCatalogSnapshotPageModel.snapshot_id == snapshot_id,
            )
            .order_by(SessionCatalogSnapshotPageModel.page_index)
        ).all()
        page_indices = tuple(int(page.page_index) for page in pages)
        expected_indices = tuple(range(payload.page_index + 1))
        if page_indices != expected_indices:
            page_index_set = set(page_indices)
            missing_index = next(
                (
                    index
                    for index in expected_indices
                    if index not in page_index_set
                ),
                0,
            )
            raise SessionCatalogRejected(
                "page_gap",
                snapshot_id=payload.snapshot_id,
                expected_page_index=missing_index,
            )
        entries: list[SessionCatalogEntry] = []
        seen: set[str] = set()
        for page in pages:
            for raw in page.sessions:
                entry = SessionCatalogEntry(
                    session_key=str(raw["session_key"]),
                    surface=str(raw["surface"]),
                    authority_revision=int(raw["authority_revision"]),
                    available_actions=tuple(raw["available_actions"]),
                )
                if entry.session_key in seen:
                    raise SessionCatalogRejected(
                        "revision_conflict",
                        snapshot_id=payload.snapshot_id,
                        expected_page_index=payload.page_index,
                    )
                seen.add(entry.session_key)
                entries.append(entry)
        session.execute(
            update(SessionCatalogEntryModel)
            .where(
                SessionCatalogEntryModel.tenant_id == tenant_id,
                SessionCatalogEntryModel.agent_id == agent_id,
                SessionCatalogEntryModel.profile == payload.profile,
                SessionCatalogEntryModel.active.is_(True),
            )
            .values(active=False, updated_at=now)
        )
        for entry in entries:
            self._upsert_entry(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                profile=payload.profile,
                runtime_generation=payload.runtime_generation,
                writer_id=device_id,
                writer_fence=authority.writer_fence,
                entry=entry,
                now=now,
            )
        session.execute(
            update(SessionCatalogGenerationModel)
            .where(
                SessionCatalogGenerationModel.tenant_id == tenant_id,
                SessionCatalogGenerationModel.agent_id == agent_id,
                SessionCatalogGenerationModel.profile == payload.profile,
                SessionCatalogGenerationModel.active.is_(True),
            )
            .values(active=False)
        )
        generation = session.get(
            SessionCatalogGenerationModel,
            (tenant_id, agent_id, payload.profile, payload.runtime_generation),
        )
        if generation is None:
            max_ordinal = session.scalar(
                select(func.max(SessionCatalogGenerationModel.ordinal)).where(
                    SessionCatalogGenerationModel.tenant_id == tenant_id,
                    SessionCatalogGenerationModel.agent_id == agent_id,
                    SessionCatalogGenerationModel.profile == payload.profile,
                )
            )
            generation = SessionCatalogGenerationModel(
                tenant_id=tenant_id,
                agent_id=agent_id,
                profile=payload.profile,
                runtime_generation=payload.runtime_generation,
                writer_id=device_id,
                writer_fence=authority.writer_fence,
                ordinal=int(max_ordinal or 0) + 1,
                active=True,
                created_at=now,
            )
            session.add(generation)
        else:
            generation.active = True
        authority.runtime_generation = payload.runtime_generation
        authority.catalog_revision = payload.catalog_revision
        authority.catalog_sequence = payload.catalog_revision
        authority.staging_snapshot_id = None
        authority.staging_runtime_generation = None
        authority.staging_catalog_revision = None
        authority.staging_deadline = None
        authority.require_full_snapshot = False
        authority.expected_page_index = 0
        authority.updated_at = now
        for page in pages:
            session.delete(page)
        return True

    def _apply_event(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        payload: ConnectorSessionCatalogEvent,
        now: datetime,
    ) -> None:
        authority = self._authority(
            session,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            profile=payload.profile,
            now=now,
            create=False,
        )
        if authority is None or authority.runtime_generation != payload.runtime_generation:
            raise SessionCatalogRejected("runtime_mismatch")
        if authority.require_full_snapshot:
            raise SessionCatalogRejected(
                "event_gap",
                expected_catalog_sequence=authority.catalog_sequence + 1,
            )
        expected = authority.catalog_sequence + 1
        if payload.catalog_sequence != expected:
            raise SessionCatalogRejected(
                "event_gap", expected_catalog_sequence=expected
            )
        stored = session.scalar(
            select(SessionCatalogEntryModel).where(
                SessionCatalogEntryModel.tenant_id == tenant_id,
                SessionCatalogEntryModel.agent_id == agent_id,
                SessionCatalogEntryModel.profile == payload.profile,
                SessionCatalogEntryModel.session_key == payload.entry.session_key,
            )
        )
        incoming_digest = canonical_payload_digest(_entry_record(payload.entry))
        if payload.action == "upsert":
            if stored is not None and (
                payload.entry.authority_revision < stored.authority_revision
                or (
                    payload.entry.authority_revision == stored.authority_revision
                    and incoming_digest != stored.content_digest
                )
            ):
                raise SessionCatalogRejected("revision_conflict")
            self._upsert_entry(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                profile=payload.profile,
                runtime_generation=payload.runtime_generation,
                writer_id=device_id,
                writer_fence=authority.writer_fence,
                entry=payload.entry,
                now=now,
            )
        else:
            if (
                stored is None
                or not stored.active
                or stored.authority_revision != payload.entry.authority_revision
                or stored.content_digest != incoming_digest
            ):
                raise SessionCatalogRejected("revision_conflict")
            stored.active = False
            stored.updated_at = now
        authority.catalog_sequence = payload.catalog_sequence
        authority.updated_at = now

    def _upsert_entry(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        profile: str,
        runtime_generation: str,
        writer_id: UUID,
        writer_fence: int,
        entry: SessionCatalogEntry,
        now: datetime,
    ) -> SessionCatalogEntryModel:
        row = session.scalar(
            select(SessionCatalogEntryModel).where(
                SessionCatalogEntryModel.tenant_id == tenant_id,
                SessionCatalogEntryModel.agent_id == agent_id,
                SessionCatalogEntryModel.profile == profile,
                SessionCatalogEntryModel.session_key == entry.session_key,
            )
        )
        digest = canonical_payload_digest(_entry_record(entry))
        if row is None:
            row = SessionCatalogEntryModel(
                tenant_id=tenant_id,
                session_id=self._id_factory(),
                workspace_id=workspace_id,
                agent_id=agent_id,
                profile=profile,
                session_key=entry.session_key,
                surface=entry.surface,
                authority_revision=entry.authority_revision,
                available_actions=list(entry.available_actions),
                runtime_generation=runtime_generation,
                writer_id=writer_id,
                writer_fence=writer_fence,
                content_digest=digest,
                active=True,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            self._ensure_projection_anchor(session, row=row, now=now)
        else:
            row.workspace_id = workspace_id
            row.surface = entry.surface
            row.authority_revision = entry.authority_revision
            row.available_actions = list(entry.available_actions)
            row.runtime_generation = runtime_generation
            row.writer_id = writer_id
            row.writer_fence = writer_fence
            row.content_digest = digest
            row.active = True
            row.updated_at = now
        return row

    def _ensure_projection_anchor(
        self,
        session: Session,
        *,
        row: SessionCatalogEntryModel,
        now: datetime,
    ) -> None:
        existing = session.scalar(
            select(SessionProjectionModel).where(
                SessionProjectionModel.tenant_id == row.tenant_id,
                SessionProjectionModel.agent_id == row.agent_id,
                SessionProjectionModel.profile == row.profile,
                SessionProjectionModel.session_key == row.session_key,
            )
        )
        if existing is not None:
            if existing.session_id != row.session_id:
                raise SessionCatalogRejected("revision_conflict")
            return
        session.add(
            SessionProjectionModel(
                tenant_id=row.tenant_id,
                session_id=row.session_id,
                session_key=row.session_key,
                workspace_id=row.workspace_id,
                agent_id=row.agent_id,
                profile=row.profile,
                title="",
                state="active",
                revision=0,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=now,
                updated_at=now,
                closed_at=None,
                retention_until=now + _ANCHOR_RETENTION,
            )
        )

    @staticmethod
    def _nack_payload(
        *,
        envelope: CloudEnvelope,
        profile: str,
        runtime_generation: str,
        digest: str,
        rejection: SessionCatalogRejected,
    ) -> dict[str, object]:
        return {
            "profile": profile,
            "runtime_generation": runtime_generation,
            "rejected_message_id": envelope.message_id,
            "rejected_payload_digest": digest,
            "rejected_connector_sequence": envelope.sequence,
            "reason": rejection.reason,
            "reset_required": True,
            "snapshot_id": rejection.snapshot_id,
            "expected_page_index": rejection.expected_page_index,
            "expected_catalog_sequence": rejection.expected_catalog_sequence,
        }


class SqlAlchemySessionCatalogRepository:
    """ACL-scoped catalog-only public read model."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def list_agent_sessions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_id: UUID,
        profile: str | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[CatalogSessionProjection, ...], int]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("catalog pagination is out of bounds")
        with self._session_factory() as session:
            membership_join = (
                WorkspaceMembershipModel.tenant_id
                == SessionCatalogEntryModel.tenant_id
            ) & (
                WorkspaceMembershipModel.workspace_id
                == SessionCatalogEntryModel.workspace_id
            )
            filters: list[object] = [
                SessionCatalogEntryModel.tenant_id == tenant_id,
                SessionCatalogEntryModel.agent_id == agent_id,
                SessionCatalogEntryModel.active.is_(True),
                WorkspaceMembershipModel.tenant_id == tenant_id,
                WorkspaceMembershipModel.user_id == user_id,
                WorkspaceMembershipModel.status == "active",
            ]
            if profile is not None:
                filters.append(SessionCatalogEntryModel.profile == profile)
            total = int(
                session.scalar(
                    select(func.count(SessionCatalogEntryModel.session_id))
                    .select_from(SessionCatalogEntryModel)
                    .join(WorkspaceMembershipModel, membership_join)
                    .where(*filters)
                )
                or 0
            )
            rows = session.scalars(
                select(SessionCatalogEntryModel)
                .join(WorkspaceMembershipModel, membership_join)
                .where(*filters)
                .order_by(
                    SessionCatalogEntryModel.updated_at.desc(),
                    SessionCatalogEntryModel.session_id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return (
                tuple(
                    CatalogSessionProjection(
                        session_id=UUID(str(row.session_id)),
                        agent_id=UUID(str(row.agent_id)),
                        workspace_id=UUID(str(row.workspace_id)),
                        profile=row.profile,
                        session_key=row.session_key,
                        runtime_generation=row.runtime_generation,
                        surface=row.surface,
                        authority_revision=row.authority_revision,
                        available_actions=tuple(row.available_actions),
                        active=row.active,
                    )
                    for row in rows
                ),
                total,
            )

    def resolve_session_id(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        profile: str,
        session_key: str,
        require_active: bool = True,
    ) -> UUID | None:
        with self._session_factory() as session:
            filters: list[object] = [
                SessionCatalogEntryModel.tenant_id == tenant_id,
                SessionCatalogEntryModel.agent_id == agent_id,
                SessionCatalogEntryModel.profile == profile,
                SessionCatalogEntryModel.session_key == session_key,
            ]
            if require_active:
                filters.append(SessionCatalogEntryModel.active.is_(True))
            value = session.scalar(
                select(SessionCatalogEntryModel.session_id).where(*filters)
            )
            return None if value is None else UUID(str(value))

    def resolve_visible_session(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_id: UUID,
        agent_id: UUID | None,
        profile: str | None,
    ) -> CatalogSessionProjection | None:
        with self._session_factory() as session:
            membership_join = (
                WorkspaceMembershipModel.tenant_id
                == SessionCatalogEntryModel.tenant_id
            ) & (
                WorkspaceMembershipModel.workspace_id
                == SessionCatalogEntryModel.workspace_id
            )
            filters: list[object] = [
                SessionCatalogEntryModel.tenant_id == tenant_id,
                SessionCatalogEntryModel.session_id == session_id,
                SessionCatalogEntryModel.active.is_(True),
                WorkspaceMembershipModel.tenant_id == tenant_id,
                WorkspaceMembershipModel.user_id == user_id,
                WorkspaceMembershipModel.status == "active",
            ]
            if agent_id is not None:
                filters.append(SessionCatalogEntryModel.agent_id == agent_id)
            if profile is not None:
                filters.append(SessionCatalogEntryModel.profile == profile)
            row = session.scalar(
                select(SessionCatalogEntryModel)
                .join(WorkspaceMembershipModel, membership_join)
                .where(*filters)
            )
            if row is None:
                return None
            return CatalogSessionProjection(
                session_id=UUID(str(row.session_id)),
                agent_id=UUID(str(row.agent_id)),
                workspace_id=UUID(str(row.workspace_id)),
                profile=row.profile,
                session_key=row.session_key,
                runtime_generation=row.runtime_generation,
                surface=row.surface,
                authority_revision=row.authority_revision,
                available_actions=tuple(row.available_actions),
                active=row.active,
            )
