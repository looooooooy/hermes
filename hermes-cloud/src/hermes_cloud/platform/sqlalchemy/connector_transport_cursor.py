"""Operation-scoped ORM authority for Connector transport cursors."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import RFC_4122, UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorResumePosition,
    ConnectorResumeResolution,
)
from hermes_cloud.platform.postgres.models import (
    ConnectorObserverReceiptModel,
    ConnectorTransportCursorModel,
    ConnectorTransportHandshakeOwnershipModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogInboxModel,
)


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


@dataclass(frozen=True, slots=True)
class LockedConnectorTransportCursor:
    tenant_id: UUID
    device_id: UUID
    connection_id: UUID
    connector_instance_id: UUID
    runtime_generation: str
    ownership_revision: int
    cursor_revision: int
    expected_next_connector_sequence: int
    expected_next_cloud_sequence: int
    resume_decision: str


def lock_active_connector_transport_epoch(
    session: Session,
    *,
    identity: ConnectorIdentity,
    connection_id: str,
    connector_instance_id: str | None,
    runtime_generation: str | None,
    now: datetime,
) -> LockedConnectorTransportCursor:
    tenant_id, device_id = _identity(identity)
    connection = _uuid(connection_id)
    instance = (
        _uuid(connector_instance_id) if connector_instance_id is not None else None
    )
    generation = (
        _string(runtime_generation, 128) if runtime_generation is not None else None
    )
    checked_at = _utc(now)
    ownership = session.get(
        ConnectorTransportHandshakeOwnershipModel,
        (tenant_id, device_id),
        with_for_update=True,
    )
    if (
        ownership is None
        or ownership.state != "active"
        or ownership.connection_id != connection
        or (instance is not None and ownership.connector_instance_id != instance)
        or (generation is not None and ownership.runtime_generation != generation)
        or _lease_expired(ownership.lease_expires_at, checked_at)
    ):
        raise RuntimeError("Connector transport cursor ownership changed")
    cursor = session.get(
        ConnectorTransportCursorModel,
        (tenant_id, device_id),
        with_for_update=True,
    )
    if (
        cursor is None
        or cursor.state != "active"
        or cursor.connection_id != connection
        or cursor.connector_instance_id != ownership.connector_instance_id
        or cursor.runtime_generation != ownership.runtime_generation
    ):
        raise RuntimeError("Connector transport cursor ownership changed")
    return LockedConnectorTransportCursor(
        tenant_id=tenant_id,
        device_id=device_id,
        connection_id=connection,
        connector_instance_id=ownership.connector_instance_id,
        runtime_generation=ownership.runtime_generation,
        ownership_revision=ownership.revision,
        cursor_revision=cursor.revision,
        expected_next_connector_sequence=cursor.next_connector_sequence,
        expected_next_cloud_sequence=cursor.next_cloud_sequence,
        resume_decision=ownership.resume_decision,
    )


def lock_connector_transport_cursor(
    session: Session,
    *,
    identity: ConnectorIdentity,
    connection_id: str,
    connector_instance_id: str,
    runtime_generation: str,
    expected_next_connector_sequence: int,
    expected_next_cloud_sequence: int,
    now: datetime,
) -> LockedConnectorTransportCursor:
    locked = lock_active_connector_transport_epoch(
        session,
        identity=identity,
        connection_id=connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation=runtime_generation,
        now=now,
    )
    if (
        locked.expected_next_connector_sequence
        != expected_next_connector_sequence
        or locked.expected_next_cloud_sequence != expected_next_cloud_sequence
    ):
        raise RuntimeError("Connector transport cursor ownership changed")
    return locked


def advance_locked_connector_transport_cursor(
    session: Session,
    *,
    locked: LockedConnectorTransportCursor,
    next_connector_sequence: int,
    next_cloud_sequence: int,
    now: datetime,
    ownership_lease: timedelta,
) -> None:
    _single_frame_advance(
        locked.expected_next_connector_sequence,
        locked.expected_next_cloud_sequence,
        next_connector_sequence,
        next_cloud_sequence,
    )
    updated_at: datetime = _utc(now)
    tenant_id: UUID = locked.tenant_id
    device_id: UUID = locked.device_id
    connection_id: UUID = locked.connection_id
    connector_instance_id: UUID = locked.connector_instance_id
    runtime_generation: str = locked.runtime_generation
    cursor_revision: int = locked.cursor_revision
    ownership_revision: int = locked.ownership_revision
    expected_next_connector_sequence: int = (
        locked.expected_next_connector_sequence
    )
    expected_next_cloud_sequence: int = locked.expected_next_cloud_sequence
    cursor_result = session.execute(
        update(ConnectorTransportCursorModel)
        .where(
            ConnectorTransportCursorModel.tenant_id == tenant_id,
            ConnectorTransportCursorModel.device_id == device_id,
            ConnectorTransportCursorModel.connection_id == connection_id,
            ConnectorTransportCursorModel.connector_instance_id
            == connector_instance_id,
            ConnectorTransportCursorModel.runtime_generation
            == runtime_generation,
            ConnectorTransportCursorModel.state == "active",
            ConnectorTransportCursorModel.revision == cursor_revision,
            ConnectorTransportCursorModel.next_connector_sequence
            == expected_next_connector_sequence,
            ConnectorTransportCursorModel.next_cloud_sequence
            == expected_next_cloud_sequence,
        )
        .values(
            next_connector_sequence=next_connector_sequence,
            next_cloud_sequence=next_cloud_sequence,
            revision=ConnectorTransportCursorModel.revision + 1,
            updated_at=updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if cursor_result.rowcount != 1:
        raise RuntimeError("Connector transport cursor ownership changed")
    ownership_result = session.execute(
        update(ConnectorTransportHandshakeOwnershipModel)
        .where(
            ConnectorTransportHandshakeOwnershipModel.tenant_id
            == tenant_id,
            ConnectorTransportHandshakeOwnershipModel.device_id
            == device_id,
            ConnectorTransportHandshakeOwnershipModel.connection_id
            == connection_id,
            ConnectorTransportHandshakeOwnershipModel.connector_instance_id
            == connector_instance_id,
            ConnectorTransportHandshakeOwnershipModel.runtime_generation
            == runtime_generation,
            ConnectorTransportHandshakeOwnershipModel.state == "active",
            ConnectorTransportHandshakeOwnershipModel.revision
            == ownership_revision,
        )
        .values(
            revision=ConnectorTransportHandshakeOwnershipModel.revision + 1,
            lease_expires_at=updated_at + ownership_lease,
            updated_at=updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if ownership_result.rowcount != 1:
        raise RuntimeError("Connector transport cursor ownership changed")


class SqlAlchemyConnectorTransportCursorAuthority:
    """Own the only durable bidirectional cursor for a Connector transport."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ownership_lease_seconds: float = 90.0,
    ) -> None:
        if not math.isfinite(ownership_lease_seconds) or ownership_lease_seconds <= 0:
            raise ValueError("Connector ownership lease must be finite and positive")
        self._session_factory = session_factory
        self._now = now
        self._ownership_lease = timedelta(seconds=ownership_lease_seconds)

    async def resolve(
        self,
        identity: ConnectorIdentity,
        position: ConnectorResumePosition,
        *,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorResumeResolution:
        return await asyncio.to_thread(
            self._resolve,
            identity,
            position,
            connector_instance_id,
            runtime_generation,
        )

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
        await asyncio.to_thread(
            self._prepare_session,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            resume_decision,
            handshake_disposition,
            previous_connection_id,
            expected_next_connector_sequence,
            expected_next_cloud_sequence,
            next_connector_sequence,
            next_cloud_sequence,
        )

    async def confirm_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        await asyncio.to_thread(
            self._confirm_session,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
        )

    async def abort_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._abort_session,
            identity,
            connection_id,
            connector_instance_id,
        )

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
        await asyncio.to_thread(
            self._commit_cursors,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            expected_next_connector_sequence,
            expected_next_cloud_sequence,
            next_connector_sequence,
            next_cloud_sequence,
        )

    async def disconnect_session(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._disconnect_session,
            identity,
            connection_id,
            connector_instance_id,
        )

    def _resolve(
        self,
        identity: ConnectorIdentity,
        position: ConnectorResumePosition,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorResumeResolution:
        tenant_id: UUID = _uuid(str(identity.tenant_id))
        device_id: UUID = _uuid(str(identity.device_id))
        instance_id: UUID = _uuid(connector_instance_id)
        generation: str = _string(runtime_generation, 128)
        _sequence(position.next_outbound_sequence)
        _sequence(position.next_inbound_sequence)
        now: datetime = _utc(self._now())
        with self._session_factory.begin() as session:
            key = (tenant_id, device_id)
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                key,
                with_for_update=True,
            )
            if ownership is not None:
                if ownership.state == "activating" and _received_welcome_proof(
                    ownership,
                    position=position,
                    connector_instance_id=instance_id,
                    runtime_generation=generation,
                ):
                    self._apply_activation(
                        session,
                        ownership=ownership,
                        target_state="offline",
                        now=now,
                    )
                    session.delete(ownership)
                    session.flush()
                elif not _lease_expired(ownership.lease_expires_at, now):
                    label = (
                        "active ownership"
                        if ownership.state == "active"
                        else "handshake ownership"
                    )
                    raise RuntimeError(f"Connector transport {label} is live")
                elif ownership.state == "active":
                    self._expire_active_ownership(
                        session,
                        ownership=ownership,
                        now=now,
                    )
                else:
                    session.delete(ownership)
                    session.flush()
            row = session.get(
                ConnectorTransportCursorModel,
                key,
                with_for_update=True,
            )
            if row is not None and row.state == "active":
                orphan_statement = (
                    update(ConnectorTransportCursorModel)
                    .where(
                        ConnectorTransportCursorModel.tenant_id == tenant_id,
                        ConnectorTransportCursorModel.device_id == device_id,
                        ConnectorTransportCursorModel.connection_id
                        == row.connection_id,
                        ConnectorTransportCursorModel.connector_instance_id
                        == row.connector_instance_id,
                        ConnectorTransportCursorModel.runtime_generation
                        == row.runtime_generation,
                        ConnectorTransportCursorModel.state == "active",
                        ConnectorTransportCursorModel.revision == row.revision,
                        ConnectorTransportCursorModel.next_connector_sequence
                        == row.next_connector_sequence,
                        ConnectorTransportCursorModel.next_cloud_sequence
                        == row.next_cloud_sequence,
                    )
                    .values(
                        state="offline",
                        revision=ConnectorTransportCursorModel.revision + 1,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                result = session.execute(orphan_statement)
                if result.rowcount != 1:
                    raise RuntimeError("Connector transport orphan ownership changed")
                session.flush()
                row = session.get(
                    ConnectorTransportCursorModel,
                    key,
                    populate_existing=True,
                    with_for_update=True,
                )
            if position.mode == "fresh":
                if row is None or (
                    row.connector_instance_id != instance_id
                    or row.runtime_generation != generation
                ):
                    disposition = (
                        "advance"
                        if position.next_outbound_sequence == 0
                        and position.next_inbound_sequence == 0
                        else "preserve"
                    )
                    return _fresh(disposition)
                return ConnectorResumeResolution(
                    "reset_required",
                    row.next_connector_sequence,
                    row.next_cloud_sequence,
                    "preserve",
                )
            if position.mode != "resume" or position.previous_connection_id is None:
                return _reset()
            if row is None:
                return _fresh("preserve")
            if (
                row.connector_instance_id != instance_id
                or row.runtime_generation != generation
            ):
                return _fresh("preserve")
            self._adopt_sent_observer_receipt_proof(
                session,
                row=row,
                position=position,
                now=now,
            )
            self._settle_catalog_receipts_from_resume_proof(
                session,
                row=row,
                position=position,
                now=now,
            )
            if (
                row.connection_id != _uuid(position.previous_connection_id)
                or row.next_connector_sequence != position.next_outbound_sequence
                or row.next_cloud_sequence != position.next_inbound_sequence
            ):
                return ConnectorResumeResolution(
                    "reset_required",
                    row.next_connector_sequence,
                    row.next_cloud_sequence,
                    "preserve",
                )
            return ConnectorResumeResolution(
                "resumed",
                row.next_connector_sequence,
                row.next_cloud_sequence,
                "advance",
            )

    @staticmethod
    def _adopt_sent_observer_receipt_proof(
        session: Session,
        *,
        row: ConnectorTransportCursorModel,
        position: ConnectorResumePosition,
        now: datetime,
    ) -> None:
        if (
            position.previous_connection_id is None
            or row.connection_id != _uuid(position.previous_connection_id)
            or position.next_outbound_sequence != row.next_connector_sequence + 1
            or position.next_inbound_sequence != row.next_cloud_sequence + 1
        ):
            return
        proof_statement = (
            select(ConnectorObserverReceiptModel)
            .where(
                ConnectorObserverReceiptModel.tenant_id == row.tenant_id,
                ConnectorObserverReceiptModel.device_id == row.device_id,
                ConnectorObserverReceiptModel.state == "pending",
                ConnectorObserverReceiptModel.dispatch_connection_id
                == row.connection_id,
                ConnectorObserverReceiptModel.dispatch_sequence
                == row.next_cloud_sequence,
                ConnectorObserverReceiptModel.sent_at.is_not(None),
            )
            .order_by(ConnectorObserverReceiptModel.observer_message_id)
            .limit(2)
            .with_for_update()
        )
        proofs = session.scalars(proof_statement).all()
        if len(proofs) != 1:
            return
        proof = proofs[0]
        if (
            proof.payload.get("observer_message_id") != str(proof.observer_message_id)
            or proof.payload.get("connector_sequence") != row.next_connector_sequence
        ):
            return
        recovery_statement = (
            update(ConnectorTransportCursorModel)
            .where(
                ConnectorTransportCursorModel.tenant_id == row.tenant_id,
                ConnectorTransportCursorModel.device_id == row.device_id,
                ConnectorTransportCursorModel.connection_id == row.connection_id,
                ConnectorTransportCursorModel.connector_instance_id
                == row.connector_instance_id,
                ConnectorTransportCursorModel.runtime_generation
                == row.runtime_generation,
                ConnectorTransportCursorModel.state == row.state,
                ConnectorTransportCursorModel.revision == row.revision,
                ConnectorTransportCursorModel.next_connector_sequence
                == row.next_connector_sequence,
                ConnectorTransportCursorModel.next_cloud_sequence
                == row.next_cloud_sequence,
            )
            .values(
                next_connector_sequence=position.next_outbound_sequence,
                next_cloud_sequence=position.next_inbound_sequence,
                revision=ConnectorTransportCursorModel.revision + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = session.execute(recovery_statement)
        if result.rowcount != 1:
            raise RuntimeError("Connector receipt recovery ownership changed")
        session.flush()
        session.refresh(row)

    @staticmethod
    def _settle_catalog_receipts_from_resume_proof(
        session: Session,
        *,
        row: ConnectorTransportCursorModel,
        position: ConnectorResumePosition,
        now: datetime,
    ) -> None:
        if (
            position.previous_connection_id is None
            or row.connection_id != _uuid(position.previous_connection_id)
            or position.next_outbound_sequence != row.next_connector_sequence
            or position.next_inbound_sequence != row.next_cloud_sequence
        ):
            return
        receipts = session.scalars(
            select(SessionCatalogInboxModel)
            .where(
                SessionCatalogInboxModel.tenant_id == row.tenant_id,
                SessionCatalogInboxModel.device_id == row.device_id,
                SessionCatalogInboxModel.receipt_state == "pending",
                SessionCatalogInboxModel.dispatch_connection_id == row.connection_id,
                SessionCatalogInboxModel.dispatch_sequence.is_not(None),
                SessionCatalogInboxModel.dispatch_sequence
                < position.next_inbound_sequence,
                SessionCatalogInboxModel.receipt_sent_at.is_not(None),
            )
            .order_by(
                SessionCatalogInboxModel.dispatch_sequence,
                SessionCatalogInboxModel.message_id,
            )
            .limit(1_024)
            .with_for_update()
        ).all()
        for receipt in receipts:
            receipt.receipt_state = "settled"
            receipt.receipt_settled_at = now
            receipt.updated_at = now

    def _prepare_session(
        self,
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
        tenant_id, device_id = _identity(identity)
        connection: UUID = _uuid(connection_id)
        instance: UUID = _uuid(connector_instance_id)
        generation: str = _string(runtime_generation, 128)
        decision: str = _resume_decision(resume_decision)
        disposition: str = _handshake_disposition(handshake_disposition)
        _decision_disposition(decision, disposition)
        now: datetime = _utc(self._now())
        lease_expires_at = now + self._ownership_lease
        with self._session_factory.begin() as session:
            key = (tenant_id, device_id)
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                key,
                with_for_update=True,
            )
            if ownership is not None:
                if not _lease_expired(ownership.lease_expires_at, now):
                    raise RuntimeError(
                        "Connector transport handshake ownership changed"
                    )
                if ownership.state == "active":
                    self._expire_active_ownership(
                        session,
                        ownership=ownership,
                        now=now,
                    )
                else:
                    session.delete(ownership)
                    session.flush()
            row = session.get(
                ConnectorTransportCursorModel,
                key,
                with_for_update=True,
            )
            if row is None:
                if decision != "fresh" or previous_connection_id is not None:
                    raise RuntimeError(
                        "Connector transport resume authority is missing"
                    )
                _new_epoch_activation(
                    disposition,
                    expected_next_connector_sequence,
                    expected_next_cloud_sequence,
                    next_connector_sequence,
                    next_cloud_sequence,
                )
            elif decision in {"fresh", "reset_required"}:
                if previous_connection_id is not None:
                    raise ValueError(
                        "Connector non-resume activation cannot name a connection"
                    )
                if row.state != "offline":
                    raise RuntimeError("Connector transport fresh ownership changed")
                same_epoch = (
                    row.connector_instance_id == instance
                    and row.runtime_generation == generation
                )
                if decision == "fresh":
                    if same_epoch:
                        raise RuntimeError(
                            "Connector transport fresh epoch did not change"
                        )
                    _new_epoch_activation(
                        disposition,
                        expected_next_connector_sequence,
                        expected_next_cloud_sequence,
                        next_connector_sequence,
                        next_cloud_sequence,
                    )
                else:
                    if not same_epoch:
                        raise RuntimeError("Connector transport reset epoch changed")
                    if (
                        row.next_connector_sequence != expected_next_connector_sequence
                        or row.next_cloud_sequence != expected_next_cloud_sequence
                    ):
                        raise RuntimeError(
                            "Connector transport reset ownership changed"
                        )
                    _reset_epoch_activation(
                        expected_next_connector_sequence,
                        expected_next_cloud_sequence,
                        next_connector_sequence,
                        next_cloud_sequence,
                    )
            else:
                if previous_connection_id is None:
                    raise ValueError(
                        "Connector resume activation requires a connection"
                    )
                if row.state != "offline":
                    raise RuntimeError("Connector transport resume ownership changed")
                _handshake_advance(
                    expected_next_connector_sequence,
                    expected_next_cloud_sequence,
                    next_connector_sequence,
                    next_cloud_sequence,
                )
                if (
                    row.connection_id != _uuid(previous_connection_id)
                    or row.connector_instance_id != instance
                    or row.runtime_generation != generation
                    or row.next_connector_sequence != expected_next_connector_sequence
                    or row.next_cloud_sequence != expected_next_cloud_sequence
                ):
                    raise RuntimeError("Connector transport resume ownership changed")
            try:
                session.add(
                    ConnectorTransportHandshakeOwnershipModel(
                        tenant_id=tenant_id,
                        device_id=device_id,
                        connector_instance_id=instance,
                        runtime_generation=generation,
                        connection_id=connection,
                        previous_connection_id=(
                            _uuid(previous_connection_id)
                            if previous_connection_id is not None
                            else None
                        ),
                        resume_decision=decision,
                        handshake_disposition=disposition,
                        state="activating",
                        expected_next_connector_sequence=(
                            expected_next_connector_sequence
                        ),
                        expected_next_cloud_sequence=expected_next_cloud_sequence,
                        next_connector_sequence=next_connector_sequence,
                        next_cloud_sequence=next_cloud_sequence,
                        revision=1,
                        lease_expires_at=lease_expires_at,
                        prepared_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
            except IntegrityError:
                raise RuntimeError(
                    "Connector transport handshake ownership changed"
                ) from None

    def _confirm_session(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        tenant_id: UUID = _uuid(str(identity.tenant_id))
        device_id: UUID = _uuid(str(identity.device_id))
        connection: UUID = _uuid(connection_id)
        instance: UUID = _uuid(connector_instance_id)
        generation: str = _string(runtime_generation, 128)
        now: datetime = _utc(self._now())
        with self._session_factory.begin() as session:
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                (tenant_id, device_id),
                with_for_update=True,
            )
            if (
                ownership is None
                or ownership.state != "activating"
                or ownership.connection_id != connection
                or ownership.connector_instance_id != instance
                or ownership.runtime_generation != generation
                or _lease_expired(ownership.lease_expires_at, now)
            ):
                raise RuntimeError("Connector transport confirmation ownership changed")
            self._apply_activation(
                session,
                ownership=ownership,
                target_state="active",
                now=now,
            )
            confirmation_statement = (
                update(ConnectorTransportHandshakeOwnershipModel)
                .where(
                    ConnectorTransportHandshakeOwnershipModel.tenant_id == tenant_id,
                    ConnectorTransportHandshakeOwnershipModel.device_id == device_id,
                    ConnectorTransportHandshakeOwnershipModel.connection_id
                    == connection,
                    ConnectorTransportHandshakeOwnershipModel.connector_instance_id
                    == instance,
                    ConnectorTransportHandshakeOwnershipModel.runtime_generation
                    == generation,
                    ConnectorTransportHandshakeOwnershipModel.state == "activating",
                    ConnectorTransportHandshakeOwnershipModel.revision
                    == ownership.revision,
                )
                .values(
                    state="active",
                    revision=ConnectorTransportHandshakeOwnershipModel.revision + 1,
                    lease_expires_at=now + self._ownership_lease,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(confirmation_statement)
            if result.rowcount != 1:
                raise RuntimeError("Connector transport confirmation ownership changed")

    def _abort_session(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        tenant_id, device_id = _identity(identity)
        connection = _uuid(connection_id)
        instance = _uuid(connector_instance_id)
        with self._session_factory.begin() as session:
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                (tenant_id, device_id),
                with_for_update=True,
            )
            if (
                ownership is not None
                and ownership.state == "activating"
                and ownership.connection_id == connection
                and ownership.connector_instance_id == instance
            ):
                session.delete(ownership)

    def _apply_activation(
        self,
        session: Session,
        *,
        ownership: ConnectorTransportHandshakeOwnershipModel,
        target_state: str,
        now: datetime,
    ) -> None:
        key = (ownership.tenant_id, ownership.device_id)
        row = session.get(
            ConnectorTransportCursorModel,
            key,
            with_for_update=True,
        )
        if row is None:
            if (
                ownership.resume_decision != "fresh"
                or ownership.previous_connection_id is not None
                or ownership.expected_next_connector_sequence != 0
                or ownership.expected_next_cloud_sequence != 0
            ):
                raise RuntimeError(
                    "Connector transport activation authority is missing"
                )
            self._retire_catalog_receipts_for_fresh_epoch(
                session,
                tenant_id=ownership.tenant_id,
                device_id=ownership.device_id,
                now=now,
            )
            session.add(
                ConnectorTransportCursorModel(
                    tenant_id=ownership.tenant_id,
                    device_id=ownership.device_id,
                    connector_instance_id=ownership.connector_instance_id,
                    runtime_generation=ownership.runtime_generation,
                    connection_id=ownership.connection_id,
                    state=target_state,
                    next_connector_sequence=ownership.next_connector_sequence,
                    next_cloud_sequence=ownership.next_cloud_sequence,
                    revision=1,
                    connected_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            return
        if row.state != "offline":
            raise RuntimeError("Connector transport activation ownership changed")
        if ownership.resume_decision == "fresh":
            if (
                row.connector_instance_id == ownership.connector_instance_id
                and row.runtime_generation == ownership.runtime_generation
            ):
                raise RuntimeError("Connector transport fresh epoch did not change")
        elif (
            row.connector_instance_id != ownership.connector_instance_id
            or row.runtime_generation != ownership.runtime_generation
            or row.next_connector_sequence != ownership.expected_next_connector_sequence
            or row.next_cloud_sequence != ownership.expected_next_cloud_sequence
            or (
                ownership.resume_decision == "resumed"
                and row.connection_id != ownership.previous_connection_id
            )
        ):
            raise RuntimeError("Connector transport activation ownership changed")
        if ownership.resume_decision == "fresh":
            self._retire_catalog_receipts_for_fresh_epoch(
                session,
                tenant_id=ownership.tenant_id,
                device_id=ownership.device_id,
                now=now,
            )
        activation_statement = (
            update(ConnectorTransportCursorModel)
            .where(
                ConnectorTransportCursorModel.tenant_id == row.tenant_id,
                ConnectorTransportCursorModel.device_id == row.device_id,
                ConnectorTransportCursorModel.connection_id == row.connection_id,
                ConnectorTransportCursorModel.connector_instance_id
                == row.connector_instance_id,
                ConnectorTransportCursorModel.runtime_generation
                == row.runtime_generation,
                ConnectorTransportCursorModel.state == "offline",
                ConnectorTransportCursorModel.revision == row.revision,
                ConnectorTransportCursorModel.next_connector_sequence
                == row.next_connector_sequence,
                ConnectorTransportCursorModel.next_cloud_sequence
                == row.next_cloud_sequence,
            )
            .values(
                connector_instance_id=ownership.connector_instance_id,
                runtime_generation=ownership.runtime_generation,
                connection_id=ownership.connection_id,
                state=target_state,
                next_connector_sequence=ownership.next_connector_sequence,
                next_cloud_sequence=ownership.next_cloud_sequence,
                revision=ConnectorTransportCursorModel.revision + 1,
                connected_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = session.execute(activation_statement)
        if result.rowcount != 1:
            raise RuntimeError("Connector transport activation ownership changed")

    def _retire_catalog_receipts_for_fresh_epoch(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        device_id: UUID,
        now: datetime,
    ) -> None:
        rows = session.scalars(
            select(SessionCatalogInboxModel)
            .where(
                SessionCatalogInboxModel.tenant_id == tenant_id,
                SessionCatalogInboxModel.device_id == device_id,
                SessionCatalogInboxModel.receipt_state == "pending",
            )
            .order_by(
                SessionCatalogInboxModel.updated_at,
                SessionCatalogInboxModel.message_id,
            )
            .with_for_update()
        ).all()
        for inbox in rows:
            inbox.receipt_state = "retired"
            inbox.receipt_retired_at = now
            inbox.receipt_retirement_reason = "connector_epoch_replaced"
            inbox.updated_at = now

    def _expire_active_ownership(
        self,
        session: Session,
        *,
        ownership: ConnectorTransportHandshakeOwnershipModel,
        now: datetime,
    ) -> None:
        tenant_id: UUID = ownership.tenant_id
        device_id: UUID = ownership.device_id
        row = session.get(
            ConnectorTransportCursorModel,
            (tenant_id, device_id),
            with_for_update=True,
        )
        if (
            row is None
            or row.state != "active"
            or row.connection_id != ownership.connection_id
            or row.connector_instance_id != ownership.connector_instance_id
        ):
            raise RuntimeError("Connector transport active ownership is inconsistent")
        connection_id: UUID = row.connection_id
        connector_instance_id: UUID = row.connector_instance_id
        runtime_generation: str = row.runtime_generation
        revision: int = row.revision
        next_connector_sequence: int = row.next_connector_sequence
        next_cloud_sequence: int = row.next_cloud_sequence
        expiry_statement = (
            update(ConnectorTransportCursorModel)
            .where(
                ConnectorTransportCursorModel.tenant_id == tenant_id,
                ConnectorTransportCursorModel.device_id == device_id,
                ConnectorTransportCursorModel.connection_id == connection_id,
                ConnectorTransportCursorModel.connector_instance_id
                == connector_instance_id,
                ConnectorTransportCursorModel.runtime_generation == runtime_generation,
                ConnectorTransportCursorModel.state == "active",
                ConnectorTransportCursorModel.revision == revision,
                ConnectorTransportCursorModel.next_connector_sequence
                == next_connector_sequence,
                ConnectorTransportCursorModel.next_cloud_sequence
                == next_cloud_sequence,
            )
            .values(
                state="offline",
                revision=ConnectorTransportCursorModel.revision + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = session.execute(expiry_statement)
        if result.rowcount != 1:
            raise RuntimeError("Connector transport active ownership changed")
        session.delete(ownership)
        session.flush()

    def _commit_cursors(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        expected_next_connector_sequence: int,
        expected_next_cloud_sequence: int,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        updated_at: datetime = _utc(self._now())
        with self._session_factory.begin() as session:
            locked = lock_connector_transport_cursor(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                expected_next_connector_sequence=expected_next_connector_sequence,
                expected_next_cloud_sequence=expected_next_cloud_sequence,
                now=updated_at,
            )
            advance_locked_connector_transport_cursor(
                session,
                locked=locked,
                next_connector_sequence=next_connector_sequence,
                next_cloud_sequence=next_cloud_sequence,
                now=updated_at,
                ownership_lease=self._ownership_lease,
            )

    def _disconnect_session(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        tenant_id: UUID = _uuid(str(identity.tenant_id))
        device_id: UUID = _uuid(str(identity.device_id))
        connection: UUID = _uuid(connection_id)
        instance: UUID = _uuid(connector_instance_id)
        updated_at: datetime = _utc(self._now())
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ConnectorTransportCursorModel)
                .where(
                    ConnectorTransportCursorModel.tenant_id == tenant_id,
                    ConnectorTransportCursorModel.device_id == device_id,
                    ConnectorTransportCursorModel.connection_id == connection,
                    ConnectorTransportCursorModel.connector_instance_id == instance,
                    ConnectorTransportCursorModel.state == "active",
                )
                .values(
                    state="offline",
                    revision=ConnectorTransportCursorModel.revision + 1,
                    updated_at=updated_at,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                return
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                (tenant_id, device_id),
                with_for_update=True,
            )
            if (
                ownership is not None
                and ownership.connection_id == connection
                and ownership.connector_instance_id == instance
            ):
                session.delete(ownership)


def _reset() -> ConnectorResumeResolution:
    return ConnectorResumeResolution("reset_required", 0, 0, "preserve")


def _fresh(handshake_disposition: str) -> ConnectorResumeResolution:
    return ConnectorResumeResolution("fresh", 0, 0, handshake_disposition)


def _identity(identity: ConnectorIdentity) -> tuple[UUID, UUID]:
    return _uuid(str(identity.tenant_id)), _uuid(str(identity.device_id))


def _uuid(value: str) -> UUID:
    parsed = UUID(value)
    if parsed.variant != RFC_4122:
        raise ValueError("Connector transport identity must be an RFC 4122 UUID")
    return parsed


def _string(value: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("Connector transport string is invalid")
    return value


def _resume_decision(value: str) -> str:
    if type(value) is not str or value not in {
        "fresh",
        "resumed",
        "reset_required",
    }:
        raise ValueError("Connector resume decision is invalid")
    return value


def _handshake_disposition(value: str) -> str:
    if type(value) is not str or value not in {"advance", "preserve"}:
        raise ValueError("Connector handshake disposition is invalid")
    return value


def _decision_disposition(decision: str, disposition: str) -> None:
    if (
        decision == "resumed"
        and disposition != "advance"
        or decision == "reset_required"
        and disposition != "preserve"
    ):
        raise ValueError("Connector resume decision and handshake disagree")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Connector transport clock must be timezone-aware")
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _lease_expired(value: datetime, now: datetime) -> bool:
    return _database_utc(value) <= now


def _received_welcome_proof(
    ownership: ConnectorTransportHandshakeOwnershipModel,
    *,
    position: ConnectorResumePosition,
    connector_instance_id: UUID,
    runtime_generation: str,
) -> bool:
    return (
        position.mode == "resume"
        and position.previous_connection_id is not None
        and _uuid(position.previous_connection_id) == ownership.connection_id
        and position.next_outbound_sequence >= ownership.next_connector_sequence
        and position.next_inbound_sequence == ownership.next_cloud_sequence
        and ownership.connector_instance_id == connector_instance_id
        and ownership.runtime_generation == runtime_generation
    )


def _sequence(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("Connector transport sequence is invalid")


def _handshake_advance(
    expected_connector: int,
    expected_cloud: int,
    next_connector: int,
    next_cloud: int,
) -> None:
    for value in (expected_connector, expected_cloud, next_connector, next_cloud):
        _sequence(value)
    if next_connector != expected_connector + 1 or next_cloud != expected_cloud + 1:
        raise ValueError("Connector handshake must advance both transport cursors")


def _new_epoch_activation(
    disposition: str,
    expected_connector: int,
    expected_cloud: int,
    next_connector: int,
    next_cloud: int,
) -> None:
    for value in (expected_connector, expected_cloud, next_connector, next_cloud):
        _sequence(value)
    expected = (0, 0, 1, 1) if disposition == "advance" else (0, 0, 0, 0)
    if (expected_connector, expected_cloud, next_connector, next_cloud) != expected:
        raise ValueError("Connector new epoch activation is inconsistent")


def _reset_epoch_activation(
    expected_connector: int,
    expected_cloud: int,
    next_connector: int,
    next_cloud: int,
) -> None:
    for value in (expected_connector, expected_cloud, next_connector, next_cloud):
        _sequence(value)
    if next_connector != expected_connector or next_cloud != expected_cloud:
        raise ValueError("Connector reset activation must preserve epoch cursors")


def _single_frame_advance(
    expected_connector: int,
    expected_cloud: int,
    next_connector: int,
    next_cloud: int,
) -> None:
    for value in (expected_connector, expected_cloud, next_connector, next_cloud):
        _sequence(value)
    delta = (next_connector - expected_connector, next_cloud - expected_cloud)
    if delta not in {(1, 0), (0, 1), (1, 1)}:
        raise ValueError("Connector cursor commit must represent one terminal frame")
