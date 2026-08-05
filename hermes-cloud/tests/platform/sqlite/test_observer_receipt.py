from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.platform.postgres.models import (
    ConnectorObserverReceiptModel,
    DeviceModel,
    TenantModel,
)
from hermes_cloud.platform.sqlalchemy import observer_receipt
from hermes_cloud.platform.sqlalchemy.observer_receipt import (
    SqlAlchemyObserverReceiptRouter,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 8, 1, 4, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEVICE_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_A = "33333333-3333-4333-8333-333333333333"
CONNECTION_B = "44444444-4444-4444-8444-444444444444"
OBSERVER_MESSAGE_ID = "55555555-5555-4555-8555-555555555555"


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _seed(path: Path, *, pending_capacity: int = 8, settled_retention: int = 8):
    engine = build_sqlite_engine(_database_url(path), allow_missing=True)
    build_sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=UUID(TENANT_ID),
                slug=f"receipt-{path.stem}",
                display_name="Receipt Test",
                status="active",
                created_at=NOW,
            )
        )
    with factory.begin() as session:
        session.add(
            DeviceModel(
                tenant_id=UUID(TENANT_ID),
                device_id=UUID(DEVICE_ID),
                agent_id=None,
                workspace_id=UUID("77777777-7777-4777-8777-777777777777"),
                device_key=f"receipt-{path.stem}-device",
                status="active",
                created_at=NOW,
            )
        )
    ids = iter(
        UUID(value)
        for value in (
            "66666666-6666-4666-8666-666666666666",
            "77777777-7777-4777-8777-777777777777",
            "88888888-8888-4888-8888-888888888888",
            "99999999-9999-4999-8999-999999999999",
        )
    )
    return (
        engine,
        factory,
        SqlAlchemyObserverReceiptRouter(
            factory,
            now=lambda: NOW,
            uuid_factory=lambda: next(ids),
            pending_capacity=pending_capacity,
            settled_retention=settled_retention,
        ),
    )


def _payload(observer_message_id: str = OBSERVER_MESSAGE_ID) -> dict[str, object]:
    return {
        "observer_message_id": observer_message_id,
        "payload_digest": "a" * 64,
        "connector_sequence": 1,
        "observer_message_type": "session.event",
        "profile": "default",
        "session_key": "session-1",
        "runtime_generation": "runtime-1",
        "runtime_session_id": "runtime-session-1",
        "event_sequence": 7,
        "committed_at": "2026-08-01T04:00:00.000Z",
    }


def test_observer_receipt_repository_has_a_dedicated_orm_module() -> None:
    assert (
        importlib.util.find_spec("hermes_cloud.platform.sqlalchemy.observer_receipt")
        is not None
    )


def test_observer_receipt_module_exposes_router_and_delivery_contract() -> None:
    assert hasattr(observer_receipt, "SqlAlchemyObserverReceiptRouter")
    assert hasattr(observer_receipt, "ConnectorObserverReceiptDelivery")


def test_pending_receipt_redelivers_on_new_connection_and_settles_only_past_cursor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine, factory, router = _seed(tmp_path / "redelivery.sqlite3")
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        first = await router.stage_and_reserve(
            identity=identity,
            connection_id=CONNECTION_A,
            observer_message_id=OBSERVER_MESSAGE_ID,
            receipt_type="stream.ack",
            payload=_payload(),
            sequence=1,
        )
        duplicate = await router.stage_and_reserve(
            identity=identity,
            connection_id=CONNECTION_A,
            observer_message_id=OBSERVER_MESSAGE_ID,
            receipt_type="stream.ack",
            payload=_payload(),
            sequence=1,
        )
        assert duplicate == first
        assert (
            await router.next_pending(
                identity=identity,
                connection_id=CONNECTION_A,
            )
            is None
        )
        pending = await router.next_pending(
            identity=identity,
            connection_id=CONNECTION_B,
        )
        assert pending == OBSERVER_MESSAGE_ID

        redelivery = await router.reserve_redelivery(
            identity=identity,
            connection_id=CONNECTION_B,
            observer_message_id=OBSERVER_MESSAGE_ID,
            sequence=5,
        )
        assert redelivery.message_id != first.message_id
        assert redelivery.observer_message_id == OBSERVER_MESSAGE_ID
        assert redelivery.sequence == 5
        assert redelivery.payload == first.payload

        assert (
            await router.confirm_through_cursor(
                identity=identity,
                connection_id=CONNECTION_A,
                durable_next_inbound_sequence=2,
            )
            == 0
        )
        assert (
            await router.confirm_through_cursor(
                identity=identity,
                connection_id=CONNECTION_B,
                durable_next_inbound_sequence=5,
            )
            == 0
        )
        assert (
            await router.confirm_through_cursor(
                identity=identity,
                connection_id=CONNECTION_B,
                durable_next_inbound_sequence=6,
            )
            == 1
        )
        with factory() as session:
            row = session.get(
                ConnectorObserverReceiptModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID), UUID(OBSERVER_MESSAGE_ID)),
            )
            assert row is not None
            assert row.state == "settled"
            assert row.dispatch_attempts == 2
        engine.dispose()

    asyncio.run(scenario())


def test_receipt_conflicts_and_pending_capacity_fail_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine, _factory, router = _seed(
            tmp_path / "capacity.sqlite3",
            pending_capacity=2,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await router.stage_and_reserve(
            identity=identity,
            connection_id=CONNECTION_A,
            observer_message_id=OBSERVER_MESSAGE_ID,
            receipt_type="stream.ack",
            payload=_payload(),
            sequence=1,
        )
        with pytest.raises(ValueError, match="binding conflicts"):
            await router.stage_and_reserve(
                identity=identity,
                connection_id=CONNECTION_A,
                observer_message_id=OBSERVER_MESSAGE_ID,
                receipt_type="stream.nack",
                payload={**_payload(), "reason": "event_gap"},
                sequence=1,
            )
        second_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        await router.stage_and_reserve(
            identity=identity,
            connection_id=CONNECTION_A,
            observer_message_id=second_id,
            receipt_type="stream.ack",
            payload=_payload(second_id),
            sequence=2,
        )
        with pytest.raises(RuntimeError, match="pending capacity"):
            await router.stage_and_reserve(
                identity=identity,
                connection_id=CONNECTION_A,
                observer_message_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                receipt_type="stream.ack",
                payload=_payload("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                sequence=3,
            )
        engine.dispose()

    asyncio.run(scenario())


def test_settled_receipt_pruning_is_bounded_and_never_deletes_pending(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine, factory, router = _seed(
            tmp_path / "retention.sqlite3",
            pending_capacity=3,
            settled_retention=1,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        ids = (
            OBSERVER_MESSAGE_ID,
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
        for sequence, observer_id in enumerate(ids, start=1):
            await router.stage_and_reserve(
                identity=identity,
                connection_id=CONNECTION_A,
                observer_message_id=observer_id,
                receipt_type="stream.ack",
                payload=_payload(observer_id),
                sequence=sequence,
            )
            assert (
                await router.confirm_through_cursor(
                    identity=identity,
                    connection_id=CONNECTION_A,
                    durable_next_inbound_sequence=sequence + 1,
                )
                == 1
            )
        pending_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        await router.stage_and_reserve(
            identity=identity,
            connection_id=CONNECTION_A,
            observer_message_id=pending_id,
            receipt_type="stream.ack",
            payload=_payload(pending_id),
            sequence=3,
        )

        with factory() as session:
            settled = session.scalar(
                select(func.count())
                .select_from(ConnectorObserverReceiptModel)
                .where(ConnectorObserverReceiptModel.state == "settled")
            )
            pending = session.get(
                ConnectorObserverReceiptModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID), UUID(pending_id)),
            )
            assert settled == 1
            assert pending is not None
            assert pending.state == "pending"
        engine.dispose()

    asyncio.run(scenario())
