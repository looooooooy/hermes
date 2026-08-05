"""Transactional ORM ingress and polling source for Connector observation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from hermes_cloud.adapters.connector_contract_v1 import (
    CloudEnvelopeV1Adapter,
    ContractConformanceError,
)
from hermes_cloud.contracts.observer_v2 import (
    ObserverV2ContractError,
    require_display_safe,
)
from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorObserverEvent,
    ConnectorObserverRejected,
    ConnectorObserverSnapshot,
)
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.domain.observer_projection_v2 import (
    ObserverProjectionV2,
    ObserverProjectionV2Error,
)
from hermes_cloud.platform.postgres.models import (
    AuditEventModel,
    DeviceLifecycleModel,
    OutboxEventModel,
    WorkspaceMembershipModel,
)
from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    ObserverEncryptionContext,
    TenantObserverCipher,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverDeletionLedgerModel,
    ObserverEventModel,
    ObserverInboxModel,
    ObserverSessionModel,
    ObserverV2StateModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogEntryModel,
)

_DEFAULT_RETENTION = timedelta(days=30)
_MAX_FUTURE_SKEW = timedelta(minutes=5)


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


class ObserverProjectionConflict(ConnectorObserverRejected):
    """The Connector attempted a conflicting or discontinuous projection write."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "projection_conflict",
        expected_event_sequence: int = 0,
        recovery: str = "stop_stream",
    ) -> None:
        super().__init__(
            reason=reason,
            expected_event_sequence=expected_event_sequence,
            recovery=recovery,
            message=message,
        )


class ObserverProjectionUnauthorized(PermissionError):
    """The Connector has no active device lifecycle for the claimed identity."""


@dataclass(frozen=True, slots=True)
class ObserverRetentionCleanupResult:
    selected: int
    deleted: int
    failed: int
    inbox_selected: int = 0
    inbox_deleted: int = 0


def _event_record(event: ConnectorObserverEvent) -> dict[str, object]:
    record: dict[str, object] = {
        "type": event.event_type,
        "session_id": event.runtime_session_id,
        "session_key": event.session_key,
        "event_sequence": event.event_sequence,
        "payload": dict(event.payload),
    }
    if event.event_sequence_start != event.event_sequence:
        record["event_sequence_start"] = event.event_sequence_start
    if event.observer_contract == 2:
        record.update(
            {
                "observer_contract": 2,
                "profile": event.profile,
                "runtime_generation": event.runtime_generation,
            }
        )
    return record


def _require_v2_display_safe_ingress(
    envelope: CloudEnvelope,
    *,
    expected_event_sequence: int,
) -> None:
    try:
        require_display_safe(envelope.payload)
    except ObserverV2ContractError as error:
        raise ObserverProjectionConflict(
            "observer v2 ingress payload is not display-safe",
            reason="projection_conflict",
            expected_event_sequence=expected_event_sequence,
            recovery="send_snapshot",
        ) from error


def _identity_ids(identity: ConnectorIdentity) -> tuple[UUID, UUID, UUID]:
    if identity.agent_id is None:
        raise ObserverProjectionUnauthorized("observer identity requires an agent")
    try:
        return (
            UUID(identity.tenant_id),
            UUID(identity.device_id),
            UUID(identity.agent_id),
        )
    except ValueError as error:
        raise ObserverProjectionUnauthorized(
            "observer identity is not tenant scoped"
        ) from error


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
        raise ObserverProjectionUnauthorized(
            "observer identity has no active device lifecycle"
        )
    return lifecycle.workspace_id


def _catalog_session_id(
    session: Session,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    profile: str,
    session_key: str,
) -> UUID:
    session_id = session.scalar(
        select(SessionCatalogEntryModel.session_id).where(
            SessionCatalogEntryModel.tenant_id == tenant_id,
            SessionCatalogEntryModel.agent_id == agent_id,
            SessionCatalogEntryModel.profile == profile,
            SessionCatalogEntryModel.session_key == session_key,
            SessionCatalogEntryModel.active.is_(True),
        )
    )
    if session_id is None:
        raise ObserverProjectionConflict(
            "observer snapshot requires an authoritative catalog identity",
            reason="runtime_binding_mismatch",
            expected_event_sequence=0,
            recovery="stop_stream",
        )
    return UUID(str(session_id))


def _register_inbox(
    session: Session,
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    device_id: UUID,
    connector_instance_id: str,
    runtime_generation: str,
    envelope: CloudEnvelope,
    payload_digest: str,
    now: datetime,
    retention_until: datetime,
) -> bool:
    message_id = UUID(envelope.message_id)
    binding = {
        "tenant_id": str(tenant_id),
        "message_id": str(message_id),
        "workspace_id": str(workspace_id),
        "agent_id": str(agent_id),
        "device_id": str(device_id),
        "connector_instance_id": connector_instance_id,
        "runtime_generation": runtime_generation,
        "connector_sequence": envelope.sequence,
        "message_type": envelope.message_type,
        "payload_digest": payload_digest,
    }
    binding_digest = canonical_payload_digest(binding)
    existing = session.get(ObserverInboxModel, (tenant_id, message_id))
    if existing is not None:
        if existing.binding_digest != binding_digest:
            raise ObserverProjectionConflict("observer inbox binding conflicts")
        return False
    sequence_binding = session.scalar(
        select(ObserverInboxModel).where(
            ObserverInboxModel.tenant_id == tenant_id,
            ObserverInboxModel.device_id == device_id,
            ObserverInboxModel.connector_instance_id == connector_instance_id,
            ObserverInboxModel.runtime_generation == runtime_generation,
            ObserverInboxModel.connector_sequence == envelope.sequence,
        )
    )
    if sequence_binding is not None:
        raise ObserverProjectionConflict("observer transport binding conflicts")
    session.add(
        ObserverInboxModel(
            tenant_id=tenant_id,
            message_id=message_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            connector_instance_id=connector_instance_id,
            runtime_generation=runtime_generation,
            connector_sequence=envelope.sequence,
            message_type=envelope.message_type,
            payload_digest=payload_digest,
            binding_digest=binding_digest,
            received_at=now,
            retention_until=retention_until,
        )
    )
    return True


def _stored_event(
    *,
    tenant_id: UUID,
    agent_id: UUID,
    session_id: UUID,
    profile: str,
    durable_session_key: str,
    event: ConnectorObserverEvent,
    source_time: datetime,
    retention_until: datetime,
    cipher: TenantObserverCipher,
) -> ObserverEventModel:
    record = _event_record(event)
    encrypted_payload = cipher.encrypt_json(
        dict(event.payload),
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=durable_session_key,
            field=f"event.payload:{event.event_sequence}",
        ),
    )
    return ObserverEventModel(
        tenant_id=tenant_id,
        session_id=session_id,
        event_sequence=event.event_sequence,
        event_sequence_start=event.event_sequence_start,
        session_key=event.session_key,
        runtime_session_id=event.runtime_session_id,
        event_type=event.event_type,
        payload=encrypted_payload,
        payload_digest=canonical_payload_digest(record),
        occurred_at=source_time,
        retention_until=retention_until,
    )


def _encryption_context(
    *,
    tenant_id: UUID,
    agent_id: UUID,
    profile: str,
    session_key: str,
    field: str,
) -> ObserverEncryptionContext:
    return ObserverEncryptionContext(
        tenant_id=tenant_id,
        agent_id=agent_id,
        profile=profile,
        session_key=session_key,
        field=field,
        schema_version=1,
    )


def _trusted_source_time(sent_at: str, now: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(sent_at)
    except ValueError as error:
        raise ObserverProjectionConflict(
            "observer source timestamp is invalid"
        ) from error
    if parsed.tzinfo is None or now.tzinfo is None:
        raise ObserverProjectionConflict("observer source timestamp must be UTC")
    source_time = parsed.astimezone(UTC)
    trusted_now = now.astimezone(UTC)
    if source_time - trusted_now > _MAX_FUTURE_SKEW:
        raise ObserverProjectionConflict(
            "observer source timestamp exceeds allowed future skew"
        )
    return min(source_time, trusted_now)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encrypted_snapshot_fields(
    *,
    cipher: TenantObserverCipher,
    tenant_id: UUID,
    agent_id: UUID,
    profile: str,
    session_key: str,
    payload: ConnectorObserverSnapshot,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    messages = cipher.encrypt_json(
        [dict(item) for item in payload.messages],
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field="messages",
        ),
    )
    inflight = cipher.encrypt_json(
        dict(payload.inflight),
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field="inflight",
        ),
    )
    replay_events = cipher.encrypt_json(
        [_event_record(item) for item in payload.replay_events],
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field="replay_events",
        ),
    )
    return messages, inflight, replay_events


def _encrypted_lifecycle_projection(
    *,
    cipher: TenantObserverCipher,
    tenant_id: UUID,
    agent_id: UUID,
    profile: str,
    session_key: str,
    payload: ConnectorObserverSnapshot,
) -> dict[str, object] | None:
    if payload.observer_contract != 2:
        return None
    return cipher.encrypt_json(
        {
            "todo_sections": [dict(item) for item in payload.todo_sections],
            "subagents": [dict(item) for item in payload.subagents],
            "tools": [dict(item) for item in payload.tools],
            "terminals": [dict(item) for item in payload.terminals],
        },
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field="lifecycle_projection",
        ),
    )


def _visible_sessions_statement(
    *,
    tenant_id: UUID,
    user_id: UUID,
    session_key: str,
    profile: str | None,
    agent_id: UUID | None = None,
):
    statement = (
        select(ObserverSessionModel)
        .join(
            WorkspaceMembershipModel,
            (WorkspaceMembershipModel.tenant_id == ObserverSessionModel.tenant_id)
            & (
                WorkspaceMembershipModel.workspace_id
                == ObserverSessionModel.workspace_id
            ),
        )
        .where(
            ObserverSessionModel.tenant_id == tenant_id,
            ObserverSessionModel.session_key == session_key,
            WorkspaceMembershipModel.user_id == user_id,
            WorkspaceMembershipModel.status == "active",
        )
        .order_by(ObserverSessionModel.profile)
    )
    if profile is not None:
        statement = statement.where(ObserverSessionModel.profile == profile)
    if agent_id is not None:
        statement = statement.where(ObserverSessionModel.agent_id == agent_id)
    return statement.limit(2)


def _outbound_event(
    event: ObserverEventModel,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    profile: str,
    runtime_generation: str,
    session_key: str,
    cipher: TenantObserverCipher,
    observer_contract: int = 1,
    public_session_id: UUID | None = None,
) -> dict[str, object]:
    payload = cipher.decrypt_json(
        event.payload,
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field=f"event.payload:{event.event_sequence}",
        ),
    )
    raw: dict[str, object] = {
        "profile": profile,
        "runtime_generation": runtime_generation,
        "type": event.event_type,
        "session_id": event.runtime_session_id,
        "session_key": event.session_key,
        "event_sequence": event.event_sequence,
        "payload": payload,
    }
    if observer_contract == 2:
        raw.update(
            {
                "observer_contract": 2,
                "profile": profile,
                "runtime_generation": runtime_generation,
            }
        )
    if event.event_sequence_start != event.event_sequence:
        raw["event_sequence_start"] = event.event_sequence_start
    try:
        decoded = CloudEnvelopeV1Adapter().decode_session_event(raw)
    except ContractConformanceError as error:
        raise ObserverProjectionConflict(
            "observer event plaintext is invalid"
        ) from error
    result = _event_record(decoded)
    if public_session_id is not None:
        result.pop("session_key", None)
        result["session_id"] = str(public_session_id)
    return result


def _stored_observer_v2_projection(
    session: Session,
    *,
    stored: ObserverSessionModel,
    stored_v2_state: ObserverV2StateModel,
    tenant_id: UUID,
    agent_id: UUID,
    profile: str,
    session_key: str,
    cipher: TenantObserverCipher,
) -> ObserverProjectionV2:
    lifecycle = cipher.decrypt_json(
        stored_v2_state.lifecycle_projection,
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field="lifecycle_projection",
        ),
    )
    replay = cipher.decrypt_json(
        stored.replay_events,
        context=_encryption_context(
            tenant_id=tenant_id,
            agent_id=agent_id,
            profile=profile,
            session_key=session_key,
            field="replay_events",
        ),
    )
    if not isinstance(lifecycle, dict) or not isinstance(replay, list):
        raise ObserverProjectionConflict(
            "observer v2 lifecycle projection is invalid",
            reason="projection_conflict",
            expected_event_sequence=int(stored.event_sequence) + 1,
            recovery="send_snapshot",
        )
    try:
        replay_events = tuple(
            CloudEnvelopeV1Adapter().decode_session_event(item) for item in replay
        )
        projection = ObserverProjectionV2.from_snapshot(
            ConnectorObserverSnapshot(
                profile=profile,
                runtime_generation=str(stored.runtime_generation),
                session_key=session_key,
                runtime_session_id=str(stored.runtime_session_id),
                running=bool(stored.running),
                status=str(stored.status),
                event_sequence=int(stored.snapshot_head_sequence),
                snapshot_event_sequence=int(stored.snapshot_event_sequence),
                messages=(),
                inflight={
                    "user": None,
                    "assistant": None,
                    "streaming": False,
                    "error": None,
                },
                replay_events=replay_events,
                todo_sections=tuple(lifecycle["todo_sections"]),
                subagents=tuple(lifecycle["subagents"]),
                tools=tuple(lifecycle["tools"]),
                terminals=tuple(lifecycle["terminals"]),
                observer_contract=2,
            )
        )
        live_events = session.scalars(
            select(ObserverEventModel)
            .where(
                ObserverEventModel.tenant_id == tenant_id,
                ObserverEventModel.session_id == UUID(str(stored.session_id)),
                ObserverEventModel.event_sequence > stored.snapshot_head_sequence,
            )
            .order_by(ObserverEventModel.event_sequence)
        ).all()
        for item in live_events:
            projection.accept(
                CloudEnvelopeV1Adapter().decode_session_event(
                    _outbound_event(
                        item,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        profile=profile,
                        runtime_generation=str(stored.runtime_generation),
                        session_key=session_key,
                        cipher=cipher,
                        observer_contract=2,
                    )
                )
            )
    except (
        ContractConformanceError,
        KeyError,
        TypeError,
        ObserverProjectionV2Error,
    ) as error:
        raise ObserverProjectionConflict(
            "observer v2 lifecycle projection conflicts",
            reason="projection_conflict",
            expected_event_sequence=int(stored.event_sequence) + 1,
            recovery="send_snapshot",
        ) from error
    return projection


class SqlAlchemyObserverIngress:
    """Persist one inbound frame and its ledgers in one ORM transaction."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        cipher: TenantObserverCipher,
        retention_policy: Callable[[UUID], timedelta | None] = (
            lambda _tenant_id: _DEFAULT_RETENTION
        ),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._retention_policy = retention_policy
        self._now = now
        self._id_factory = id_factory

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
        await asyncio.to_thread(
            self._accept_snapshot,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
            payload,
        )

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
        await asyncio.to_thread(
            self._accept_event,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
            envelope,
            payload,
        )

    def _accept_snapshot(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorObserverSnapshot,
    ) -> None:
        tenant_id, device_id, agent_id = _identity_ids(identity)
        profile = str(payload.profile)
        session_key = str(payload.session_key)
        if payload.observer_contract == 2:
            _require_v2_display_safe_ingress(
                envelope,
                expected_event_sequence=payload.snapshot_event_sequence,
            )
            try:
                ObserverProjectionV2.from_snapshot(payload)
            except ObserverProjectionV2Error as error:
                raise ObserverProjectionConflict(
                    "observer v2 lifecycle projection conflicts",
                    reason="projection_conflict",
                    expected_event_sequence=payload.snapshot_event_sequence,
                    recovery="send_snapshot",
                ) from error
        now = self._now()
        source_time = _trusted_source_time(envelope.sent_at, now)
        retention_period = self._retention_policy(tenant_id)
        if retention_period is None:
            raise ObserverProjectionUnauthorized(
                "observer retention is disabled for the tenant"
            )
        if retention_period <= timedelta(0):
            raise ObserverProjectionConflict("observer retention policy is invalid")
        retention_until = source_time + retention_period
        with self._session_factory.begin() as session:
            workspace_id = _active_workspace(
                session,
                tenant_id=tenant_id,
                device_id=device_id,
                agent_id=agent_id,
            )
            catalog_session_id = _catalog_session_id(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                profile=profile,
                session_key=session_key,
            )
            digest = canonical_payload_digest(envelope.payload)
            if not _register_inbox(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                envelope=envelope,
                payload_digest=digest,
                now=now,
                retention_until=retention_until,
            ):
                return
            stored = session.scalar(
                select(ObserverSessionModel).where(
                    ObserverSessionModel.tenant_id == tenant_id,
                    ObserverSessionModel.agent_id == agent_id,
                    ObserverSessionModel.profile == profile,
                    ObserverSessionModel.session_key == session_key,
                )
            )
            if stored is None:
                messages, inflight, replay_events = _encrypted_snapshot_fields(
                    cipher=self._cipher,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=profile,
                    session_key=session_key,
                    payload=payload,
                )
                stored = ObserverSessionModel(
                    tenant_id=tenant_id,
                    session_id=catalog_session_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile=profile,
                    session_key=session_key,
                    runtime_session_id=payload.runtime_session_id,
                    runtime_generation=runtime_generation,
                    connector_instance_id=connector_instance_id,
                    connection_id=connection_id,
                    running=payload.running,
                    status=payload.status,
                    event_sequence=payload.event_sequence,
                    snapshot_event_sequence=payload.snapshot_event_sequence,
                    snapshot_head_sequence=payload.event_sequence,
                    messages=messages,
                    inflight=inflight,
                    replay_events=replay_events,
                    payload_digest=digest,
                    updated_at=now,
                    retention_until=retention_until,
                )
                session.add(stored)
                session.flush()
            else:
                if UUID(str(stored.session_id)) != catalog_session_id:
                    raise ObserverProjectionConflict(
                        "observer projection conflicts with catalog identity",
                        reason="runtime_binding_mismatch",
                        expected_event_sequence=int(stored.event_sequence) + 1,
                        recovery="stop_stream",
                    )
                same_generation = stored.runtime_generation == runtime_generation
                stored_v2_state = session.get(
                    ObserverV2StateModel,
                    (tenant_id, UUID(str(stored.session_id))),
                )
                stored_observer_contract = 2 if stored_v2_state is not None else 1
                if (
                    same_generation
                    and stored_observer_contract != payload.observer_contract
                ):
                    raise ObserverProjectionConflict(
                        "observer snapshot contract conflicts with the runtime binding",
                        reason="runtime_binding_mismatch",
                        expected_event_sequence=stored.event_sequence + 1,
                    )
                if same_generation and (
                    stored.runtime_session_id != payload.runtime_session_id
                ):
                    raise ObserverProjectionConflict(
                        "observer snapshot runtime binding conflicts",
                        reason="runtime_binding_mismatch",
                        expected_event_sequence=stored.event_sequence + 1,
                    )
                if same_generation and payload.event_sequence < stored.event_sequence:
                    raise ObserverProjectionConflict(
                        "observer snapshot cannot regress the event sequence"
                    )
                if same_generation and payload.event_sequence == stored.event_sequence:
                    if stored.payload_digest != digest:
                        raise ObserverProjectionConflict(
                            "observer snapshot cursor has conflicting content"
                        )
                    return
                messages, inflight, replay_events = _encrypted_snapshot_fields(
                    cipher=self._cipher,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=profile,
                    session_key=session_key,
                    payload=payload,
                )
                stored.workspace_id = workspace_id
                stored.device_id = device_id
                stored.runtime_session_id = payload.runtime_session_id
                stored.runtime_generation = runtime_generation
                stored.connector_instance_id = connector_instance_id
                stored.connection_id = connection_id
                stored.running = payload.running
                stored.status = payload.status
                stored.event_sequence = payload.event_sequence
                stored.snapshot_event_sequence = payload.snapshot_event_sequence
                stored.snapshot_head_sequence = payload.event_sequence
                stored.messages = messages
                stored.inflight = inflight
                stored.replay_events = replay_events
                stored.payload_digest = digest
                stored.updated_at = now
                stored.retention_until = retention_until
            stored_session_id = UUID(str(stored.session_id))
            stored_v2_state = session.get(
                ObserverV2StateModel,
                (tenant_id, stored_session_id),
            )
            lifecycle_projection = _encrypted_lifecycle_projection(
                cipher=self._cipher,
                tenant_id=tenant_id,
                agent_id=agent_id,
                profile=profile,
                session_key=session_key,
                payload=payload,
            )
            if lifecycle_projection is None:
                if stored_v2_state is not None:
                    session.delete(stored_v2_state)
            elif stored_v2_state is None:
                session.add(
                    ObserverV2StateModel(
                        tenant_id=tenant_id,
                        session_id=stored_session_id,
                        observer_contract=2,
                        lifecycle_projection=lifecycle_projection,
                    )
                )
            else:
                stored_v2_state.lifecycle_projection = lifecycle_projection
            session.execute(
                delete(ObserverEventModel).where(
                    ObserverEventModel.tenant_id == tenant_id,
                    ObserverEventModel.session_id == stored_session_id,
                )
            )
            for replay in payload.replay_events:
                session.add(
                    _stored_event(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        session_id=stored_session_id,
                        profile=profile,
                        durable_session_key=session_key,
                        event=replay,
                        source_time=source_time,
                        retention_until=retention_until,
                        cipher=self._cipher,
                    )
                )
            self._metadata_ledgers(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                session_id=stored_session_id,
                message_type="session.snapshot",
                profile=profile,
                session_key=session_key,
                event_sequence=payload.event_sequence,
                now=now,
            )

    def _accept_event(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: ConnectorObserverEvent,
    ) -> None:
        tenant_id, device_id, agent_id = _identity_ids(identity)
        profile = str(payload.profile)
        session_key = str(payload.session_key)
        event_sequence = int(payload.event_sequence)
        if payload.observer_contract == 2:
            _require_v2_display_safe_ingress(
                envelope,
                expected_event_sequence=event_sequence,
            )
        now = self._now()
        source_time = _trusted_source_time(envelope.sent_at, now)
        retention_period = self._retention_policy(tenant_id)
        if retention_period is None:
            raise ObserverProjectionUnauthorized(
                "observer retention is disabled for the tenant"
            )
        if retention_period <= timedelta(0):
            raise ObserverProjectionConflict("observer retention policy is invalid")
        retention_until = source_time + retention_period
        with self._session_factory.begin() as session:
            workspace_id = _active_workspace(
                session,
                tenant_id=tenant_id,
                device_id=device_id,
                agent_id=agent_id,
            )
            digest = canonical_payload_digest(envelope.payload)
            if not _register_inbox(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                envelope=envelope,
                payload_digest=digest,
                now=now,
                retention_until=retention_until,
            ):
                return
            stored = session.scalar(
                select(ObserverSessionModel).where(
                    ObserverSessionModel.tenant_id == tenant_id,
                    ObserverSessionModel.agent_id == agent_id,
                    ObserverSessionModel.profile == profile,
                    ObserverSessionModel.session_key == session_key,
                )
            )
            if stored is None:
                raise ObserverProjectionConflict(
                    "observer event requires an authoritative snapshot",
                    expected_event_sequence=0,
                    recovery="send_snapshot",
                )
            if (
                stored.runtime_generation != runtime_generation
                or stored.runtime_session_id != payload.runtime_session_id
            ):
                raise ObserverProjectionConflict(
                    "observer event runtime binding does not match the snapshot",
                    reason="runtime_binding_mismatch",
                    expected_event_sequence=stored.event_sequence + 1,
                )
            stored_session_id = UUID(str(stored.session_id))
            stored_v2_state = session.get(
                ObserverV2StateModel,
                (tenant_id, stored_session_id),
            )
            stored_observer_contract = 2 if stored_v2_state is not None else 1
            if stored_observer_contract != payload.observer_contract:
                raise ObserverProjectionConflict(
                    "observer event contract does not match the snapshot",
                    reason="runtime_binding_mismatch",
                    expected_event_sequence=stored.event_sequence + 1,
                )
            existing = session.scalar(
                select(ObserverEventModel).where(
                    ObserverEventModel.tenant_id == tenant_id,
                    ObserverEventModel.session_id == stored_session_id,
                    ObserverEventModel.event_sequence == event_sequence,
                )
            )
            event_digest = canonical_payload_digest(_event_record(payload))
            if existing is not None:
                if existing.payload_digest != event_digest:
                    raise ObserverProjectionConflict(
                        "observer event sequence has conflicting content"
                    )
                return
            if payload.event_sequence_start != stored.event_sequence + 1:
                raise ObserverProjectionConflict(
                    "observer event sequence must be contiguous",
                    reason="event_gap",
                    expected_event_sequence=stored.event_sequence + 1,
                    recovery="send_snapshot",
                )
            if stored_v2_state is not None:
                projection = _stored_observer_v2_projection(
                    session,
                    stored=stored,
                    stored_v2_state=stored_v2_state,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=profile,
                    session_key=session_key,
                    cipher=self._cipher,
                )
                try:
                    projection.accept(payload)
                except ObserverProjectionV2Error as error:
                    raise ObserverProjectionConflict(
                        "observer v2 lifecycle projection conflicts",
                        reason="projection_conflict",
                        expected_event_sequence=stored.event_sequence + 1,
                        recovery="send_snapshot",
                    ) from error
            session.add(
                _stored_event(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=stored_session_id,
                    profile=profile,
                    durable_session_key=session_key,
                    event=payload,
                    source_time=source_time,
                    retention_until=retention_until,
                    cipher=self._cipher,
                )
            )
            stored.event_sequence = payload.event_sequence
            stored.device_id = device_id
            stored.connector_instance_id = connector_instance_id
            stored.connection_id = connection_id
            stored.updated_at = now
            stored.retention_until = max(
                _utc(stored.retention_until),
                retention_until,
            )
            if payload.event_type == "status.update":
                status = payload.payload.get("status")
                running = payload.payload.get("running")
                if isinstance(status, str) and isinstance(running, bool):
                    stored.status = status
                    stored.running = running
            self._metadata_ledgers(
                session,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                session_id=stored_session_id,
                message_type="session.event",
                profile=profile,
                session_key=session_key,
                event_sequence=event_sequence,
                now=now,
            )

    def _metadata_ledgers(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        session_id: UUID,
        message_type: str,
        profile: str,
        session_key: str,
        event_sequence: int,
        now: datetime,
    ) -> None:
        metadata = {
            "profile": profile,
            "session_key": session_key,
            "event_sequence": event_sequence,
        }
        session.add(
            OutboxEventModel(
                tenant_id=tenant_id,
                event_id=self._id_factory(),
                workspace_id=workspace_id,
                aggregate_type="observer_session",
                aggregate_id=session_id,
                event_type=f"observer.{message_type}.accepted",
                payload=metadata,
                state="pending",
                publish_attempts=0,
                available_at=now,
                published_at=None,
                created_at=now,
            )
        )
        session.add(
            AuditEventModel(
                tenant_id=tenant_id,
                audit_event_id=self._id_factory(),
                workspace_id=workspace_id,
                actor_type="agent",
                actor_id=agent_id,
                purpose="project connector observer state",
                action=message_type,
                subject_type="observer_session",
                subject_id=session_id,
                outcome="accepted",
                details=metadata,
                occurred_at=now,
            )
        )


class SqlAlchemyObserverRetentionCleaner:
    """Bounded, retryable ORM cleanup for expired encrypted projections."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        tenant_id: UUID,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch_size: int = 100,
        retry_delay: timedelta = timedelta(minutes=5),
        claim_lease: timedelta = timedelta(minutes=5),
        before_delete: Callable[[UUID, UUID], None] = lambda _tenant, _session: None,
        before_inbox_delete: Callable[[UUID, UUID], None] = (
            lambda _tenant, _message: None
        ),
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not 1 <= batch_size <= 500:
            raise ValueError("observer cleanup batch size must be between 1 and 500")
        if retry_delay <= timedelta(0) or claim_lease <= timedelta(0):
            raise ValueError("observer cleanup retry timing must be positive")
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._now = now
        self._batch_size = batch_size
        self._retry_delay = retry_delay
        self._claim_lease = claim_lease
        self._before_delete = before_delete
        self._before_inbox_delete = before_inbox_delete
        self._id_factory = id_factory

    def run_once(self) -> ObserverRetentionCleanupResult:
        now = self._now()
        inbox_selected, inbox_deleted = self._delete_expired_inbox(now=now)
        with self._session_factory.begin() as session:
            candidates = session.scalars(
                select(ObserverSessionModel)
                .outerjoin(
                    ObserverDeletionLedgerModel,
                    (
                        ObserverDeletionLedgerModel.tenant_id
                        == ObserverSessionModel.tenant_id
                    )
                    & (
                        ObserverDeletionLedgerModel.session_id
                        == ObserverSessionModel.session_id
                    ),
                )
                .where(
                    ObserverSessionModel.tenant_id == self._tenant_id,
                    ObserverSessionModel.retention_until <= now,
                    or_(
                        ObserverDeletionLedgerModel.session_id.is_(None),
                        (ObserverDeletionLedgerModel.state != "deleted")
                        & (ObserverDeletionLedgerModel.available_at <= now),
                    ),
                )
                .order_by(
                    ObserverSessionModel.retention_until,
                    ObserverSessionModel.session_id,
                )
                .limit(self._batch_size)
            ).all()
            identities = tuple(
                (UUID(str(item.tenant_id)), UUID(str(item.session_id)))
                for item in candidates
            )

        deleted_count = 0
        failed_count = 0
        for tenant_id, session_id in identities:
            if not self._claim(tenant_id=tenant_id, session_id=session_id, now=now):
                continue
            try:
                self._delete(tenant_id=tenant_id, session_id=session_id, now=now)
            except Exception:  # noqa: BLE001 - retry ledger stores only a safe code
                failed_count += 1
                self._record_failure(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    now=now,
                )
            else:
                deleted_count += 1
        return ObserverRetentionCleanupResult(
            selected=len(identities),
            deleted=deleted_count,
            failed=failed_count,
            inbox_selected=inbox_selected,
            inbox_deleted=inbox_deleted,
        )

    def _delete_expired_inbox(self, *, now: datetime) -> tuple[int, int]:
        with self._session_factory.begin() as session:
            candidates = session.scalars(
                select(ObserverInboxModel)
                .where(
                    ObserverInboxModel.tenant_id == self._tenant_id,
                    ObserverInboxModel.retention_until <= now,
                )
                .order_by(
                    ObserverInboxModel.retention_until,
                    ObserverInboxModel.message_id,
                )
                .limit(self._batch_size)
            ).all()
            for candidate in candidates:
                message_id = UUID(str(candidate.message_id))
                self._before_inbox_delete(self._tenant_id, message_id)
                session.delete(candidate)
            session.flush()
            return len(candidates), len(candidates)

    def _claim(self, *, tenant_id: UUID, session_id: UUID, now: datetime) -> bool:
        with self._session_factory.begin() as session:
            stored = session.get(ObserverSessionModel, (tenant_id, session_id))
            if stored is None or _utc(stored.retention_until) > _utc(now):
                return False
            ledger = session.get(ObserverDeletionLedgerModel, (tenant_id, session_id))
            if ledger is not None and (
                ledger.state == "deleted" or _utc(ledger.available_at) > _utc(now)
            ):
                return False
            if ledger is None:
                ledger = ObserverDeletionLedgerModel(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    workspace_id=stored.workspace_id,
                    agent_id=stored.agent_id,
                    profile=stored.profile,
                    session_key=stored.session_key,
                    state="pending",
                    attempts=1,
                    available_at=now + self._claim_lease,
                    last_error_code=None,
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                session.add(ledger)
            else:
                ledger.state = "pending"
                ledger.attempts += 1
                ledger.available_at = now + self._claim_lease
                ledger.last_error_code = None
                ledger.updated_at = now
            session.flush()
            return True

    def _delete(self, *, tenant_id: UUID, session_id: UUID, now: datetime) -> None:
        with self._session_factory.begin() as session:
            ledger = session.get(ObserverDeletionLedgerModel, (tenant_id, session_id))
            stored = session.get(ObserverSessionModel, (tenant_id, session_id))
            if ledger is None or ledger.state != "pending":
                raise RuntimeError("observer deletion claim is unavailable")
            if stored is not None:
                if _utc(stored.retention_until) > _utc(now):
                    raise RuntimeError("observer projection is not expired")
                self._before_delete(tenant_id, session_id)
                session.execute(
                    delete(ObserverEventModel).where(
                        ObserverEventModel.tenant_id == tenant_id,
                        ObserverEventModel.session_id == session_id,
                    )
                )
                session.execute(
                    delete(ObserverV2StateModel).where(
                        ObserverV2StateModel.tenant_id == tenant_id,
                        ObserverV2StateModel.session_id == session_id,
                    )
                )
                session.delete(stored)
                session.add(
                    AuditEventModel(
                        tenant_id=tenant_id,
                        audit_event_id=self._id_factory(),
                        workspace_id=ledger.workspace_id,
                        actor_type="system",
                        actor_id=None,
                        purpose="apply observer retention policy",
                        action="retention.delete",
                        subject_type="observer_session",
                        subject_id=session_id,
                        outcome="accepted",
                        details={
                            "profile": ledger.profile,
                            "session_key": ledger.session_key,
                        },
                        occurred_at=now,
                    )
                )
            ledger.state = "deleted"
            ledger.available_at = now
            ledger.last_error_code = None
            ledger.updated_at = now
            ledger.deleted_at = now
            session.flush()

    def _record_failure(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        now: datetime,
    ) -> None:
        with self._session_factory.begin() as session:
            ledger = session.get(ObserverDeletionLedgerModel, (tenant_id, session_id))
            if ledger is None or ledger.state == "deleted":
                return
            ledger.state = "failed"
            ledger.available_at = now + self._retry_delay
            ledger.last_error_code = "observer_retention_delete_failed"
            ledger.updated_at = now
            session.flush()


class SqlAlchemyObserverProjectionRepository:
    """ACL-aware ORM queries for snapshot and event batches."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        cipher: TenantObserverCipher,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    def observer_snapshot(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: str | None = None,
        agent_id: UUID | None = None,
    ) -> Mapping[str, object] | None:
        with self._session_factory.begin() as session:
            candidates = session.scalars(
                _visible_sessions_statement(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key=session_key,
                    profile=profile,
                    agent_id=agent_id,
                )
            ).all()
            if len(candidates) != 1:
                return None
            stored = candidates[0]
            stored_session_id = UUID(str(stored.session_id))
            agent_id = UUID(str(stored.agent_id))
            stored_profile = str(stored.profile)
            stored_session_key = str(stored.session_key)
            snapshot_head_sequence = int(stored.snapshot_head_sequence)
            stored_v2_state = session.get(
                ObserverV2StateModel,
                (tenant_id, stored_session_id),
            )
            observer_contract = 2 if stored_v2_state is not None else 1
            live_events = session.scalars(
                select(ObserverEventModel)
                .where(
                    ObserverEventModel.tenant_id == tenant_id,
                    ObserverEventModel.session_id == stored_session_id,
                    ObserverEventModel.event_sequence > snapshot_head_sequence,
                )
                .order_by(ObserverEventModel.event_sequence)
            ).all()
            messages = self._cipher.decrypt_json(
                stored.messages,
                context=_encryption_context(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=stored_profile,
                    session_key=stored_session_key,
                    field="messages",
                ),
            )
            inflight = self._cipher.decrypt_json(
                stored.inflight,
                context=_encryption_context(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=stored_profile,
                    session_key=stored_session_key,
                    field="inflight",
                ),
            )
            replay_events = self._cipher.decrypt_json(
                stored.replay_events,
                context=_encryption_context(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=stored_profile,
                    session_key=stored_session_key,
                    field="replay_events",
                ),
            )
            raw_snapshot: dict[str, object] = {
                "profile": stored_profile,
                "runtime_generation": str(stored.runtime_generation),
                "session_key": stored_session_key,
                "runtime_session_id": str(stored.runtime_session_id),
                "running": bool(stored.running),
                "status": str(stored.status),
                "event_sequence": snapshot_head_sequence,
                "snapshot_event_sequence": int(stored.snapshot_event_sequence),
                "messages": messages,
                "inflight": inflight,
                "replay_events": replay_events,
            }
            lifecycle_projection: object = None
            if stored_v2_state is not None:
                lifecycle_projection = self._cipher.decrypt_json(
                    stored_v2_state.lifecycle_projection,
                    context=_encryption_context(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        profile=stored_profile,
                        session_key=stored_session_key,
                        field="lifecycle_projection",
                    ),
                )
                if not isinstance(lifecycle_projection, dict):
                    raise ObserverProjectionConflict(
                        "observer v2 lifecycle projection is invalid"
                    )
                raw_snapshot.update(
                    {
                        "observer_contract": 2,
                        **lifecycle_projection,
                    }
                )
            try:
                decoded_snapshot = CloudEnvelopeV1Adapter().decode_session_snapshot(
                    raw_snapshot
                )
            except ContractConformanceError as error:
                raise ObserverProjectionConflict(
                    "observer snapshot plaintext is invalid"
                ) from error
            result = {
                "session_key": stored.session_key,
                "runtime_session_id": stored.runtime_session_id,
                "running": stored.running,
                "status": stored.status,
                "event_sequence": stored.event_sequence,
                "snapshot_event_sequence": stored.snapshot_event_sequence,
                "messages": [dict(item) for item in decoded_snapshot.messages],
                "inflight": dict(decoded_snapshot.inflight),
                "replay_events": [
                    *[_event_record(item) for item in decoded_snapshot.replay_events],
                    *[
                        _outbound_event(
                            item,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            profile=stored_profile,
                            runtime_generation=str(stored.runtime_generation),
                            session_key=stored_session_key,
                            cipher=self._cipher,
                            observer_contract=observer_contract,
                            public_session_id=stored_session_id,
                        )
                        for item in live_events
                    ],
                ],
            }
            if observer_contract == 2:
                assert isinstance(lifecycle_projection, dict)
                result.update(
                    {
                        "observer_contract": 2,
                        "profile": stored_profile,
                        "runtime_generation": str(stored.runtime_generation),
                        "session_id": str(stored_session_id),
                        **lifecycle_projection,
                    }
                )
                result.pop("session_key", None)
                result.pop("runtime_session_id", None)
            return result

    def event_batch(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: str | None,
        after_sequence: int,
        limit: int,
        agent_id: UUID | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        with self._session_factory.begin() as session:
            candidates = session.scalars(
                _visible_sessions_statement(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key=session_key,
                    profile=profile,
                    agent_id=agent_id,
                )
            ).all()
            if len(candidates) != 1:
                return ()
            stored = candidates[0]
            stored_session_id = UUID(str(stored.session_id))
            agent_id = UUID(str(stored.agent_id))
            stored_profile = str(stored.profile)
            stored_session_key = str(stored.session_key)
            stored_v2_state = session.get(
                ObserverV2StateModel,
                (tenant_id, stored_session_id),
            )
            observer_contract = 2 if stored_v2_state is not None else 1
            events = session.scalars(
                select(ObserverEventModel)
                .where(
                    ObserverEventModel.tenant_id == tenant_id,
                    ObserverEventModel.session_id == stored_session_id,
                    ObserverEventModel.event_sequence > after_sequence,
                )
                .order_by(ObserverEventModel.event_sequence)
                .limit(limit)
            ).all()
            return tuple(
                _outbound_event(
                    item,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    profile=stored_profile,
                    runtime_generation=str(stored.runtime_generation),
                    session_key=stored_session_key,
                    cipher=self._cipher,
                    observer_contract=observer_contract,
                    public_session_id=(
                        stored_session_id if observer_contract == 2 else None
                    ),
                )
                for item in events
            )


class ObserverProjectionEventSource:
    """Bounded cancellable polling over committed ORM journal rows."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        cipher: TenantObserverCipher,
        poll_interval_seconds: float = 0.1,
        batch_size: int = 100,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if not 1 <= batch_size <= 500:
            raise ValueError("batch size must be between 1 and 500")
        self._repository = SqlAlchemyObserverProjectionRepository(
            session_factory,
            cipher=cipher,
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size

    async def events(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: str | None,
        after_sequence: int,
        agent_id: UUID | None = None,
    ) -> AsyncIterator[Mapping[str, object]]:
        cursor = after_sequence
        while True:
            batch = await asyncio.to_thread(
                self._repository.event_batch,
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                profile=profile,
                after_sequence=cursor,
                limit=self._batch_size,
                agent_id=agent_id,
            )
            if not batch:
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            for event in batch:
                cursor = int(event["event_sequence"])
                yield event
