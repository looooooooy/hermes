"""Durable ORM coordinator and Connector router for Observer subscriptions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from threading import Lock, RLock
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorObserverSubscriptionDelivery,
)
from hermes_cloud.modules.cloud_api.domain import (
    ObserverSubscription,
    ObserverSubscriptionCapacityExceeded,
    Principal,
)
from hermes_cloud.platform.postgres.models import (
    AuditEventModel,
    DeviceLifecycleModel,
    OutboxEventModel,
    SessionProjectionModel,
    WorkspaceMembershipModel,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverSessionModel,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverConnectorRouteModel,
    ObserverSubscriptionIntentModel,
    ObserverSubscriptionLeaseModel,
    ObserverSubscriptionTargetModel,
)


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


class ObserverSubscriptionUnauthorized(PermissionError):
    """The principal cannot resolve one exact active Agent/device target."""


class _ConnectorCapacityLockEntry:
    def __init__(self) -> None:
        self.lock = RLock()
        self.users = 0


_CONNECTOR_CAPACITY_LOCKS: dict[
    tuple[UUID, UUID],
    _ConnectorCapacityLockEntry,
] = {}
_CONNECTOR_CAPACITY_LOCKS_GUARD = Lock()


@contextmanager
def _connector_capacity_lock(
    tenant_id: UUID,
    device_id: UUID,
) -> Iterator[None]:
    key = (tenant_id, device_id)
    with _CONNECTOR_CAPACITY_LOCKS_GUARD:
        entry = _CONNECTOR_CAPACITY_LOCKS.get(key)
        if entry is None:
            entry = _ConnectorCapacityLockEntry()
            _CONNECTOR_CAPACITY_LOCKS[key] = entry
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _CONNECTOR_CAPACITY_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0:
                current = _CONNECTOR_CAPACITY_LOCKS.pop(key, None)
                if current is not entry:
                    raise RuntimeError(
                        "Observer capacity lock registry is inconsistent"
                    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SqlAlchemyObserverSubscriptionRouter:
    """Aggregate client leases and route fixed intents through durable ORM facts."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], UUID] = uuid4,
        lease_ttl_seconds: int = 90,
        poll_interval_seconds: float = 0.1,
        max_active_targets_per_connector: int = 32,
    ) -> None:
        if not 30 <= lease_ttl_seconds <= 600:
            raise ValueError("Observer lease TTL must be between 30 and 600 seconds")
        if not 0 < poll_interval_seconds <= 5:
            raise ValueError("Observer intent poll interval is invalid")
        if not 1 <= max_active_targets_per_connector <= 64:
            raise ValueError("Observer active target limit is invalid")
        self._session_factory = session_factory
        self._now = now
        self._id_factory = id_factory
        self._lease_ttl = timedelta(seconds=lease_ttl_seconds)
        self._poll_interval_seconds = poll_interval_seconds
        self._max_active_targets_per_connector = max_active_targets_per_connector

    def open_subscription(
        self,
        *,
        principal: Principal,
        session_key: str,
        profile: str | None,
        agent_id: UUID | None = None,
    ) -> ObserverSubscription:
        last_error: Exception | None = None
        for delay in (0.0, 0.005, 0.01, 0.02, 0.04):
            if delay:
                time.sleep(delay)
            try:
                return self._open_subscription_once(
                    principal=principal,
                    session_key=session_key,
                    profile=profile,
                    agent_id=agent_id,
                )
            except (IntegrityError, OperationalError, StaleDataError) as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _open_subscription_once(
        self,
        *,
        principal: Principal,
        session_key: str,
        profile: str | None,
        agent_id: UUID | None = None,
    ) -> ObserverSubscription:
        if not 1 <= len(session_key) <= 256 or session_key != session_key.strip():
            raise ObserverSubscriptionUnauthorized
        if profile is not None and (
            not 1 <= len(profile) <= 128 or profile != profile.strip()
        ):
            raise ObserverSubscriptionUnauthorized
        client_subscription_id = self._id_factory()
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            self._expire_stale_leases(session, now)
            workspace_id, agent_id, device_id, resolved_profile = (
                self._authorized_target(
                    session,
                    principal=principal,
                    session_key=session_key,
                    profile=profile,
                    agent_id=agent_id,
                )
            )
            resolved_profile = str(resolved_profile)
            with _connector_capacity_lock(principal.tenant_id, device_id):
                return self._open_authorized_subscription(
                    session,
                    principal=principal,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    session_key=session_key,
                    profile=resolved_profile,
                    client_subscription_id=client_subscription_id,
                    now=now,
                )

    def _open_authorized_subscription(
        self,
        session: Session,
        *,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        device_id: UUID,
        session_key: str,
        profile: str,
        client_subscription_id: UUID,
        now: datetime,
    ) -> ObserverSubscription:
        target = session.scalar(
            select(ObserverSubscriptionTargetModel).where(
                ObserverSubscriptionTargetModel.tenant_id == principal.tenant_id,
                ObserverSubscriptionTargetModel.agent_id == agent_id,
                ObserverSubscriptionTargetModel.profile == profile,
                ObserverSubscriptionTargetModel.session_key == session_key,
            )
        )
        requires_initial_snapshot = target is None or target.active_ref_count == 0
        if requires_initial_snapshot:
            active_targets = session.scalars(
                select(ObserverSubscriptionTargetModel)
                .where(
                    ObserverSubscriptionTargetModel.tenant_id == principal.tenant_id,
                    ObserverSubscriptionTargetModel.device_id == device_id,
                    ObserverSubscriptionTargetModel.state == "active",
                    ObserverSubscriptionTargetModel.active_ref_count > 0,
                )
                .with_for_update()
            ).all()
            if len(active_targets) >= self._max_active_targets_per_connector:
                raise ObserverSubscriptionCapacityExceeded
        if target is None:
            target = ObserverSubscriptionTargetModel(
                tenant_id=principal.tenant_id,
                target_subscription_id=self._id_factory(),
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                profile=profile,
                session_key=session_key,
                state="active",
                active_ref_count=0,
                next_intent_sequence=0,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(target)
            session.flush()
        if target.state != "active":
            self._cancel_unsettled_close(session, target, now)
            target.state = "active"
        target.active_ref_count += 1
        target.updated_at = now
        session.add(
            ObserverSubscriptionLeaseModel(
                tenant_id=principal.tenant_id,
                client_subscription_id=client_subscription_id,
                target_subscription_id=target.target_subscription_id,
                user_id=principal.user_id,
                state="active",
                expires_at=now + self._lease_ttl,
                created_at=now,
                updated_at=now,
                closed_at=None,
            )
        )
        if requires_initial_snapshot:
            self._append_intent(
                session,
                target=target,
                message_type="session.observe.open",
                reason=None,
                now=now,
            )
        session.flush()
        return ObserverSubscription(
            subscription_id=client_subscription_id,
            target_subscription_id=target.target_subscription_id,
            session_key=session_key,
            profile=profile,
            requires_initial_snapshot=requires_initial_snapshot,
        )

    def close_subscription(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
        reason: str,
    ) -> None:
        if reason not in {
            "client_unsubscribe",
            "subscription_replaced",
            "authorization_revoked",
            "gateway_shutdown",
            "reconciliation",
        }:
            raise ValueError("Observer close reason is invalid")
        last_error: Exception | None = None
        for delay in (0.0, 0.005, 0.01, 0.02, 0.04):
            if delay:
                time.sleep(delay)
            try:
                self._close_subscription_once(
                    principal=principal,
                    subscription_id=subscription_id,
                    reason=reason,
                )
                return
            except (IntegrityError, OperationalError, StaleDataError) as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _close_subscription_once(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
        reason: str,
    ) -> None:
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            self._expire_stale_leases(session, now)
            lease = session.get(
                ObserverSubscriptionLeaseModel,
                (principal.tenant_id, subscription_id),
            )
            if lease is None or lease.user_id != principal.user_id:
                raise ObserverSubscriptionUnauthorized
            if lease.state != "active":
                return
            target = session.get(
                ObserverSubscriptionTargetModel,
                (principal.tenant_id, lease.target_subscription_id),
            )
            if target is None or target.active_ref_count <= 0:
                raise RuntimeError("Observer target reference count is invalid")
            lease.state = "closed"
            lease.closed_at = now
            lease.updated_at = now
            target.active_ref_count -= 1
            target.updated_at = now
            if target.active_ref_count == 0:
                target.state = "closing"
                self._append_intent(
                    session,
                    target=target,
                    message_type="session.observe.close",
                    reason=reason,
                    now=now,
                )
            session.flush()

    def snapshot_ready(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
    ) -> bool:
        with self._session_factory.begin() as session:
            lease = session.get(
                ObserverSubscriptionLeaseModel,
                (principal.tenant_id, subscription_id),
            )
            if (
                lease is None
                or lease.user_id != principal.user_id
                or lease.state != "active"
            ):
                raise ObserverSubscriptionUnauthorized
            target = session.get(
                ObserverSubscriptionTargetModel,
                (principal.tenant_id, lease.target_subscription_id),
            )
            if target is None:
                raise ObserverSubscriptionUnauthorized
            open_intent = session.scalar(
                select(ObserverSubscriptionIntentModel)
                .where(
                    ObserverSubscriptionIntentModel.tenant_id == principal.tenant_id,
                    ObserverSubscriptionIntentModel.target_subscription_id
                    == target.target_subscription_id,
                    ObserverSubscriptionIntentModel.message_type
                    == "session.observe.open",
                    ObserverSubscriptionIntentModel.state != "cancelled",
                )
                .order_by(
                    ObserverSubscriptionIntentModel.intent_sequence.desc(),
                    ObserverSubscriptionIntentModel.request_id.desc(),
                )
                .limit(1)
            )
            if open_intent is None:
                return False
            snapshot = session.scalar(
                select(ObserverSessionModel).where(
                    ObserverSessionModel.tenant_id == principal.tenant_id,
                    ObserverSessionModel.workspace_id == target.workspace_id,
                    ObserverSessionModel.agent_id == target.agent_id,
                    ObserverSessionModel.device_id == target.device_id,
                    ObserverSessionModel.profile == target.profile,
                    ObserverSessionModel.session_key == target.session_key,
                    ObserverSessionModel.updated_at >= open_intent.created_at,
                )
            )
            return snapshot is not None

    def renew_subscription(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
    ) -> None:
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ObserverSubscriptionLeaseModel)
                .where(
                    ObserverSubscriptionLeaseModel.tenant_id == principal.tenant_id,
                    ObserverSubscriptionLeaseModel.client_subscription_id
                    == subscription_id,
                    ObserverSubscriptionLeaseModel.user_id == principal.user_id,
                    ObserverSubscriptionLeaseModel.state == "active",
                )
                .values(
                    expires_at=now + self._lease_ttl,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise ObserverSubscriptionUnauthorized

    def expire_stale_leases(self) -> None:
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            self._expire_stale_leases(session, now)
            session.flush()

    async def connector_connected(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        await asyncio.to_thread(
            self._connector_connected,
            identity,
            connection_id,
            connector_instance_id,
            runtime_generation,
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

    async def wait_for_subscription_intent(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorObserverSubscriptionDelivery | None:
        while True:
            delivery = await asyncio.to_thread(
                self._next_subscription_intent,
                identity,
                connection_id,
                connector_instance_id,
                runtime_generation,
            )
            if delivery is not None:
                return delivery
            await asyncio.sleep(self._poll_interval_seconds)

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
    ) -> ConnectorObserverSubscriptionDelivery:
        return await asyncio.to_thread(
            self._reserve_subscription_intent,
            identity,
            connection_id,
            connector_instance_id,
            request_id,
            message_id,
            sequence,
            observer_contract,
            wire_message_type,
            wire_payload_digest,
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

    def _connector_connected(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> None:
        tenant_id, device_id, agent_id = self._connector_identity(identity)
        UUID(connection_id)
        UUID(connector_instance_id)
        if not 1 <= len(runtime_generation) <= 128:
            raise RuntimeError("Connector runtime generation is invalid")
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            lifecycle = session.scalar(
                select(DeviceLifecycleModel).where(
                    DeviceLifecycleModel.tenant_id == tenant_id,
                    DeviceLifecycleModel.device_id == device_id,
                    DeviceLifecycleModel.agent_id == agent_id,
                    DeviceLifecycleModel.state == "active",
                )
            )
            if lifecycle is None:
                raise ObserverSubscriptionUnauthorized
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, device_id),
            )
            if route is None:
                session.add(
                    ObserverConnectorRouteModel(
                        tenant_id=tenant_id,
                        device_id=device_id,
                        agent_id=agent_id,
                        connector_instance_id=connector_instance_id,
                        connection_id=connection_id,
                        runtime_generation=runtime_generation,
                        state="active",
                        next_connector_sequence=0,
                        next_cloud_sequence=0,
                        revision=1,
                        connected_at=now,
                        updated_at=now,
                    )
                )
            else:
                route.agent_id = agent_id
                route.connector_instance_id = connector_instance_id
                route.connection_id = connection_id
                route.runtime_generation = runtime_generation
                route.state = "active"
                route.next_connector_sequence = 0
                route.next_cloud_sequence = 0
                route.connected_at = now
                route.updated_at = now
            session.flush()

    def _connector_disconnected(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            tenant_id, device_id, _agent_id = self._connector_identity(identity)
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, device_id),
            )
            if (
                route is not None
                and route.connection_id == connection_id
                and route.connector_instance_id == connector_instance_id
            ):
                route.state = "offline"
                route.updated_at = now
                session.flush()

    def _next_subscription_intent(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorObserverSubscriptionDelivery | None:
        tenant_id, device_id, agent_id = self._connector_identity(identity)
        with self._session_factory.begin() as session:
            self._expire_stale_leases(session, self._now().astimezone(UTC))
            self._require_route(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            )
            active_target_exists = exists().where(
                ObserverSubscriptionTargetModel.tenant_id
                == ObserverSubscriptionIntentModel.tenant_id,
                ObserverSubscriptionTargetModel.target_subscription_id
                == ObserverSubscriptionIntentModel.target_subscription_id,
                ObserverSubscriptionTargetModel.state == "active",
                ObserverSubscriptionTargetModel.active_ref_count > 0,
            )
            intent = session.scalar(
                select(ObserverSubscriptionIntentModel)
                .where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id,
                    ObserverSubscriptionIntentModel.device_id == device_id,
                    ObserverSubscriptionIntentModel.agent_id == agent_id,
                    or_(
                        ObserverSubscriptionIntentModel.dispatch_connection_id.is_(
                            None
                        ),
                        ObserverSubscriptionIntentModel.dispatch_connection_id
                        != connection_id,
                    ),
                    or_(
                        and_(
                            ObserverSubscriptionIntentModel.message_type
                            == "session.observe.close",
                            ObserverSubscriptionIntentModel.state.in_(
                                ("pending", "dispatching")
                            ),
                        ),
                        and_(
                            ObserverSubscriptionIntentModel.message_type
                            == "session.observe.open",
                            ObserverSubscriptionIntentModel.state.in_(
                                ("pending", "dispatching", "settled")
                            ),
                            active_target_exists,
                        ),
                    ),
                )
                .order_by(
                    ObserverSubscriptionIntentModel.created_at,
                    ObserverSubscriptionIntentModel.intent_sequence,
                    ObserverSubscriptionIntentModel.request_id,
                )
                .limit(1)
            )
            if intent is not None:
                return self._delivery(intent)
        return None

    def _expire_stale_leases(self, session: Session, now: datetime) -> None:
        leases = session.scalars(
            select(ObserverSubscriptionLeaseModel)
            .where(
                ObserverSubscriptionLeaseModel.state == "active",
                ObserverSubscriptionLeaseModel.expires_at <= now,
            )
            .with_for_update()
        ).all()
        for lease in leases:
            result = session.execute(
                update(ObserverSubscriptionLeaseModel)
                .where(
                    ObserverSubscriptionLeaseModel.tenant_id == lease.tenant_id,
                    ObserverSubscriptionLeaseModel.client_subscription_id
                    == lease.client_subscription_id,
                    ObserverSubscriptionLeaseModel.state == "active",
                    ObserverSubscriptionLeaseModel.expires_at <= now,
                )
                .values(
                    state="expired",
                    closed_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                continue
            if result.rowcount != 1:
                raise RuntimeError("Observer lease expiry update was not singular")
            target = session.get(
                ObserverSubscriptionTargetModel,
                (lease.tenant_id, lease.target_subscription_id),
                with_for_update=True,
            )
            if target is None or target.active_ref_count <= 0:
                raise RuntimeError("Observer expired lease target is invalid")
            target.active_ref_count -= 1
            target.updated_at = now
            if target.active_ref_count == 0 and target.state == "active":
                target.state = "closing"
                self._append_intent(
                    session,
                    target=target,
                    message_type="session.observe.close",
                    reason="reconciliation",
                    now=now,
                )

    def _reserve_subscription_intent(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        request_id: str,
        message_id: str,
        sequence: int,
        observer_contract: int,
        wire_message_type: str,
        wire_payload_digest: str,
    ) -> ConnectorObserverSubscriptionDelivery:
        if request_id != message_id or type(sequence) is not int or sequence < 0:
            raise RuntimeError("Observer reservation identity is invalid")
        tenant_id, device_id, agent_id = self._connector_identity(identity)
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            route = self._require_route(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=None,
            )
            intent = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, UUID(request_id)),
            )
            if (
                intent is None
                or intent.device_id != device_id
                or intent.agent_id != agent_id
                or intent.state == "cancelled"
            ):
                raise RuntimeError("Observer reservation target changed")
            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, intent.target_subscription_id),
                with_for_update=True,
            )
            if target is None or (
                intent.message_type == "session.observe.open"
                and (target.state != "active" or target.active_ref_count <= 0)
            ):
                raise RuntimeError("Observer reservation target changed")
            self._require_or_freeze_wire_binding(
                intent,
                observer_contract=observer_contract,
                wire_message_type=wire_message_type,
                wire_payload_digest=wire_payload_digest,
            )
            if (
                intent.state == "dispatching"
                and intent.dispatch_connection_id == connection_id
                and intent.dispatch_sequence == sequence
            ):
                return self._delivery(intent)
            if intent.dispatch_connection_id == connection_id:
                raise RuntimeError("Observer intent is already dispatched")
            if intent.dispatch_sequence is not None and not (
                intent.state == "dispatching" and intent.dispatch_sequence == sequence
            ):
                previous = intent
                if previous.state == "dispatching":
                    previous.state = "cancelled"
                    previous.updated_at = now
                    previous_outbox = session.get(
                        OutboxEventModel,
                        (tenant_id, previous.request_id),
                    )
                    if previous_outbox is None:
                        raise RuntimeError("Observer intent outbox fact is missing")
                    previous_outbox.state = "dead"
                intent = self._append_intent(
                    session,
                    target=target,
                    message_type=previous.message_type,
                    reason=(
                        str(previous.payload["reason"])
                        if previous.message_type == "session.observe.close"
                        else None
                    ),
                    now=now,
                    supersedes_request_id=previous.request_id,
                )
                self._freeze_derived_wire_binding(
                    intent,
                    observer_contract=observer_contract,
                )
                session.flush()
            intent.state = "dispatching"
            intent.dispatch_connection_id = connection_id
            intent.dispatch_sequence = sequence
            intent.dispatch_attempts += 1
            intent.dispatched_at = now
            intent.settled_at = None
            intent.updated_at = now
            outbox = session.get(OutboxEventModel, (tenant_id, intent.request_id))
            if outbox is None:
                raise RuntimeError("Observer intent outbox fact is missing")
            outbox.state = "publishing"
            outbox.publish_attempts += 1
            outbox.available_at = now
            outbox.published_at = None
            route.updated_at = now
            session.flush()
            return self._delivery(intent)

    def _connector_heartbeat(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        if (
            type(next_connector_sequence) is not int
            or next_connector_sequence < 0
            or type(next_cloud_sequence) is not int
            or next_cloud_sequence < 0
        ):
            raise RuntimeError("Connector heartbeat cursor is invalid")
        tenant_id, _device_id, _agent_id = self._connector_identity(identity)
        now = self._now().astimezone(UTC)
        with self._session_factory.begin() as session:
            route = self._require_route(
                session,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            )
            if (
                next_connector_sequence < route.next_connector_sequence
                or next_cloud_sequence < route.next_cloud_sequence
            ):
                raise RuntimeError("Connector heartbeat cursor regressed")
            route.next_connector_sequence = next_connector_sequence
            route.next_cloud_sequence = next_cloud_sequence
            route.updated_at = now
            dispatched = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id,
                    ObserverSubscriptionIntentModel.state == "dispatching",
                    ObserverSubscriptionIntentModel.dispatch_connection_id
                    == connection_id,
                    ObserverSubscriptionIntentModel.dispatch_sequence
                    < next_cloud_sequence,
                )
            ).all()
            for intent in dispatched:
                intent.state = "settled"
                intent.settled_at = now
                intent.updated_at = now
                outbox = session.get(
                    OutboxEventModel,
                    (tenant_id, intent.request_id),
                )
                if outbox is None:
                    raise RuntimeError("Observer intent outbox fact is missing")
                outbox.state = "published"
                outbox.published_at = now
                if intent.message_type == "session.observe.close":
                    target = session.get(
                        ObserverSubscriptionTargetModel,
                        (tenant_id, intent.target_subscription_id),
                    )
                    if target is not None and target.active_ref_count == 0:
                        target.state = "closed"
                        target.updated_at = now
            session.flush()

    @staticmethod
    def _connector_identity(identity: ConnectorIdentity) -> tuple[UUID, UUID, UUID]:
        if identity.agent_id is None or "session.observe" not in identity.scopes:
            raise ObserverSubscriptionUnauthorized
        try:
            return (
                UUID(identity.tenant_id),
                UUID(identity.device_id),
                UUID(identity.agent_id),
            )
        except ValueError as error:
            raise ObserverSubscriptionUnauthorized from error

    @staticmethod
    def _require_route(
        session: Session,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str | None,
    ) -> ObserverConnectorRouteModel:
        tenant_id, device_id, agent_id = (
            SqlAlchemyObserverSubscriptionRouter._connector_identity(identity)
        )
        route = session.get(
            ObserverConnectorRouteModel,
            (tenant_id, device_id),
        )
        if (
            route is None
            or route.agent_id != agent_id
            or route.state != "active"
            or route.connection_id != connection_id
            or route.connector_instance_id != connector_instance_id
            or (
                runtime_generation is not None
                and route.runtime_generation != runtime_generation
            )
        ):
            raise RuntimeError("Connector Observer route is no longer authoritative")
        return route

    @staticmethod
    def _delivery(
        intent: ObserverSubscriptionIntentModel,
    ) -> ConnectorObserverSubscriptionDelivery:
        timestamp_field = (
            "requested_at"
            if intent.message_type == "session.observe.open"
            else "closed_at"
        )
        return ConnectorObserverSubscriptionDelivery(
            request_id=str(intent.request_id),
            message_id=str(intent.request_id),
            message_type=intent.message_type,
            sent_at=str(intent.payload[timestamp_field]),
            payload=dict(intent.payload),
            observer_contract=intent.observer_contract,
            wire_message_type=intent.wire_message_type,
            wire_payload_digest=intent.wire_payload_digest,
        )

    @staticmethod
    def _wire_binding(
        intent: ObserverSubscriptionIntentModel,
        *,
        observer_contract: int,
    ) -> tuple[str, str]:
        if type(observer_contract) is not int or observer_contract not in {1, 2}:
            raise RuntimeError("Observer intent wire contract is invalid")
        wire_message_type = (
            f"{intent.message_type}.v2"
            if observer_contract == 2
            else intent.message_type
        )
        wire_payload = {
            **dict(intent.payload),
            **({"observer_contract": 2} if observer_contract == 2 else {}),
        }
        return wire_message_type, canonical_payload_digest(wire_payload)

    @classmethod
    def _require_or_freeze_wire_binding(
        cls,
        intent: ObserverSubscriptionIntentModel,
        *,
        observer_contract: int,
        wire_message_type: str,
        wire_payload_digest: str,
    ) -> None:
        expected_message_type, expected_payload_digest = cls._wire_binding(
            intent,
            observer_contract=observer_contract,
        )
        if (
            wire_message_type != expected_message_type
            or wire_payload_digest != expected_payload_digest
        ):
            raise RuntimeError("Observer intent wire contract is invalid")
        stored = (
            intent.observer_contract,
            intent.wire_message_type,
            intent.wire_payload_digest,
        )
        if stored == (None, None, None):
            intent.observer_contract = observer_contract
            intent.wire_message_type = wire_message_type
            intent.wire_payload_digest = wire_payload_digest
            return
        if stored != (
            observer_contract,
            wire_message_type,
            wire_payload_digest,
        ):
            raise RuntimeError("Observer intent wire contract changed")

    @classmethod
    def _freeze_derived_wire_binding(
        cls,
        intent: ObserverSubscriptionIntentModel,
        *,
        observer_contract: int,
    ) -> None:
        wire_message_type, wire_payload_digest = cls._wire_binding(
            intent,
            observer_contract=observer_contract,
        )
        intent.observer_contract = observer_contract
        intent.wire_message_type = wire_message_type
        intent.wire_payload_digest = wire_payload_digest

    def _authorized_target(
        self,
        session: Session,
        *,
        principal: Principal,
        session_key: str,
        profile: str | None,
        agent_id: UUID | None = None,
    ) -> tuple[UUID, UUID, UUID, str]:
        projection_query = (
            select(SessionProjectionModel)
            .join(
                WorkspaceMembershipModel,
                (WorkspaceMembershipModel.tenant_id == SessionProjectionModel.tenant_id)
                & (
                    WorkspaceMembershipModel.workspace_id
                    == SessionProjectionModel.workspace_id
                ),
            )
            .where(
                SessionProjectionModel.tenant_id == principal.tenant_id,
                SessionProjectionModel.session_key == session_key,
                WorkspaceMembershipModel.user_id == principal.user_id,
                WorkspaceMembershipModel.status == "active",
            )
        )
        if agent_id is not None:
            projection_query = projection_query.where(
                SessionProjectionModel.agent_id == agent_id
            )
        if profile is not None:
            projection_query = projection_query.where(
                SessionProjectionModel.profile == profile
            )
        projections = session.scalars(
            projection_query.distinct()
            .order_by(SessionProjectionModel.session_id)
            .limit(2)
        ).all()
        if len(projections) != 1 or projections[0].agent_id is None:
            raise ObserverSubscriptionUnauthorized
        projection = projections[0]
        devices = session.scalars(
            select(DeviceLifecycleModel)
            .where(
                DeviceLifecycleModel.tenant_id == principal.tenant_id,
                DeviceLifecycleModel.workspace_id == projection.workspace_id,
                DeviceLifecycleModel.agent_id == projection.agent_id,
                DeviceLifecycleModel.state == "active",
            )
            .with_for_update()
        ).all()
        if len(devices) != 1:
            raise ObserverSubscriptionUnauthorized
        resolved_profile = profile
        if resolved_profile is None:
            profiles = session.scalars(
                select(ObserverSessionModel.profile)
                .where(
                    ObserverSessionModel.tenant_id == principal.tenant_id,
                    ObserverSessionModel.agent_id == projection.agent_id,
                    ObserverSessionModel.session_key == session_key,
                )
                .order_by(ObserverSessionModel.profile)
                .limit(2)
            ).all()
            if not profiles:
                raise ObserverSubscriptionUnauthorized(
                    "Observer session profile not found"
                )
            if len(profiles) != 1:
                raise ObserverSubscriptionUnauthorized(
                    "Observer session profile is ambiguous"
                )
            resolved_profile = str(profiles[0])
        return (
            projection.workspace_id,
            projection.agent_id,
            devices[0].device_id,
            resolved_profile,
        )

    def _append_intent(
        self,
        session: Session,
        *,
        target: ObserverSubscriptionTargetModel,
        message_type: str,
        reason: str | None,
        now: datetime,
        supersedes_request_id: UUID | None = None,
    ) -> ObserverSubscriptionIntentModel:
        request_id = self._id_factory()
        intent_sequence = target.next_intent_sequence
        target.next_intent_sequence += 1
        payload: dict[str, object] = {
            "request_id": str(request_id),
            "subscription_id": str(target.target_subscription_id),
            "profile": target.profile,
            "session_key": target.session_key,
            "target_source": "cloud_authorized_binding",
        }
        if message_type == "session.observe.open":
            payload["requested_at"] = _timestamp(now)
        else:
            payload["reason"] = reason
            payload["closed_at"] = _timestamp(now)
        intent = ObserverSubscriptionIntentModel(
            tenant_id=target.tenant_id,
            request_id=request_id,
            supersedes_request_id=supersedes_request_id,
            target_subscription_id=target.target_subscription_id,
            intent_sequence=intent_sequence,
            workspace_id=target.workspace_id,
            agent_id=target.agent_id,
            device_id=target.device_id,
            message_type=message_type,
            payload=payload,
            state="pending",
            dispatch_connection_id=None,
            dispatch_sequence=None,
            dispatch_attempts=0,
            dispatched_at=None,
            settled_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(intent)
        session.add(
            OutboxEventModel(
                tenant_id=target.tenant_id,
                event_id=request_id,
                workspace_id=target.workspace_id,
                aggregate_type="observer_subscription",
                aggregate_id=target.target_subscription_id,
                event_type=message_type,
                payload={
                    "request_id": str(request_id),
                    "subscription_id": str(target.target_subscription_id),
                    "agent_id": str(target.agent_id),
                    "device_id": str(target.device_id),
                    "profile": target.profile,
                    "session_key": target.session_key,
                },
                state="pending",
                publish_attempts=0,
                available_at=now,
                published_at=None,
                created_at=now,
            )
        )
        session.add(
            AuditEventModel(
                tenant_id=target.tenant_id,
                audit_event_id=self._id_factory(),
                workspace_id=target.workspace_id,
                actor_type="system",
                actor_id=None,
                purpose="route authorized observer subscription",
                action=message_type,
                subject_type="observer_subscription",
                subject_id=target.target_subscription_id,
                outcome="accepted",
                details={
                    "request_id": str(request_id),
                    "profile": target.profile,
                    "session_key": target.session_key,
                },
                occurred_at=now,
            )
        )
        return intent

    @staticmethod
    def _cancel_unsettled_close(
        session: Session,
        target: ObserverSubscriptionTargetModel,
        now: datetime,
    ) -> None:
        closes = session.scalars(
            select(ObserverSubscriptionIntentModel).where(
                ObserverSubscriptionIntentModel.tenant_id == target.tenant_id,
                ObserverSubscriptionIntentModel.target_subscription_id
                == target.target_subscription_id,
                ObserverSubscriptionIntentModel.message_type == "session.observe.close",
                ObserverSubscriptionIntentModel.state.in_(("pending", "dispatching")),
            )
        ).all()
        for close in closes:
            close.state = "cancelled"
            close.updated_at = now
            outbox = session.get(
                OutboxEventModel,
                (target.tenant_id, close.request_id),
            )
            if outbox is None:
                raise RuntimeError("Observer close outbox fact is missing")
            outbox.state = "dead"
            outbox.available_at = now
