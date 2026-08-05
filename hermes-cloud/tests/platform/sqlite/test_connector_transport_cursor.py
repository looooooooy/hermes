from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorResumePosition,
)
from hermes_cloud.platform.postgres.models import (
    ConnectorObserverReceiptModel,
    ConnectorTransportCursorModel,
    ConnectorTransportHandshakeOwnershipModel,
    DeviceModel,
    TenantModel,
)
from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (
    SqlAlchemyConnectorTransportCursorAuthority,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogInboxModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEVICE_ID = "22222222-2222-4222-8222-222222222222"
CONNECTOR_ID = "33333333-3333-4333-8333-333333333333"
CONNECTION_ID = "44444444-4444-4444-8444-444444444444"
REPLACEMENT_ID = "55555555-5555-4555-8555-555555555555"
RUNTIME_GENERATION = "runtime-generation-1"


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _seed_authority(
    path: Path,
    *,
    clock: MutableClock,
    lease_seconds: float = 90.0,
):
    engine = build_sqlite_engine(_database_url(path), allow_missing=True)
    build_sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=UUID(TENANT_ID),
                slug=f"transport-{path.stem}",
                display_name="Transport Test",
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
                device_key=f"transport-{path.stem}-device",
                status="active",
                created_at=NOW,
            )
        )
    authority = SqlAlchemyConnectorTransportCursorAuthority(
        factory,
        now=clock,
        ownership_lease_seconds=lease_seconds,
    )
    return engine, factory, authority


async def _prepare_and_confirm(
    authority: SqlAlchemyConnectorTransportCursorAuthority,
    *,
    identity: ConnectorIdentity,
    connection_id: str,
    connector_instance_id: str,
    resume_decision: str,
    previous_connection_id: str | None,
    expected_next_connector_sequence: int,
    expected_next_cloud_sequence: int,
    next_connector_sequence: int,
    next_cloud_sequence: int,
) -> None:
    await authority.prepare_session(
        identity=identity,
        connection_id=connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation=RUNTIME_GENERATION,
        resume_decision=resume_decision,
        handshake_disposition="advance",
        previous_connection_id=previous_connection_id,
        expected_next_connector_sequence=expected_next_connector_sequence,
        expected_next_cloud_sequence=expected_next_cloud_sequence,
        next_connector_sequence=next_connector_sequence,
        next_cloud_sequence=next_cloud_sequence,
    )
    await authority.confirm_session(
        identity=identity,
        connection_id=connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation=RUNTIME_GENERATION,
    )


def test_prepare_confirm_disconnect_is_two_phase_and_preserves_cursor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, factory, authority = _seed_authority(
            tmp_path / "two-phase.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)

        await authority.prepare_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            resume_decision="fresh",
            handshake_disposition="advance",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        with factory() as session:
            assert (
                session.get(
                    ConnectorTransportCursorModel,
                    (UUID(TENANT_ID), UUID(DEVICE_ID)),
                )
                is None
            )
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert ownership is not None
            assert ownership.state == "activating"
            assert ownership.next_connector_sequence == 1
            assert ownership.next_cloud_sequence == 1

        await authority.confirm_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        with factory() as session:
            cursor = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            ownership = session.get(
                ConnectorTransportHandshakeOwnershipModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert cursor is not None
            assert cursor.state == "active"
            assert (cursor.next_connector_sequence, cursor.next_cloud_sequence) == (
                1,
                1,
            )
            assert ownership is not None
            assert ownership.state == "active"

        await authority.disconnect_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
        )
        with factory() as session:
            cursor = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert cursor is not None
            assert cursor.state == "offline"
            assert (cursor.next_connector_sequence, cursor.next_cloud_sequence) == (
                1,
                1,
            )
            assert (
                session.get(
                    ConnectorTransportHandshakeOwnershipModel,
                    (UUID(TENANT_ID), UUID(DEVICE_ID)),
                )
                is None
            )
        engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("resume_next_inbound", "expected_decision", "expected_state"),
    ((2, "resumed", "settled"), (1, "reset_required", "pending")),
)
def test_resume_cursor_settles_only_exact_catalog_dispatch_ownership(
    tmp_path: Path,
    resume_next_inbound: int,
    expected_decision: str,
    expected_state: str,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, factory, authority = _seed_authority(
            tmp_path / f"catalog-resume-{resume_next_inbound}.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await _prepare_and_confirm(
            authority,
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            resume_decision="fresh",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        catalog_message_id = UUID("66666666-6666-4666-8666-666666666661")
        with factory.begin() as session:
            cursor = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert cursor is not None
            cursor.next_connector_sequence = 2
            cursor.next_cloud_sequence = 2
            cursor.revision += 1
            cursor.updated_at = NOW
            session.add(
                SessionCatalogInboxModel(
                    tenant_id=UUID(TENANT_ID),
                    message_id=catalog_message_id,
                    workspace_id=UUID("77777777-7777-4777-8777-777777777777"),
                    agent_id=UUID("88888888-8888-4888-8888-888888888888"),
                    device_id=UUID(DEVICE_ID),
                    connector_instance_id=UUID(CONNECTOR_ID),
                    runtime_generation=RUNTIME_GENERATION,
                    connector_sequence=1,
                    message_type="session.catalog.snapshot.page",
                    payload_digest="a" * 64,
                    receipt_type="session.catalog.ack",
                    receipt_payload={"acked_message_id": str(catalog_message_id)},
                    receipt_state="pending",
                    dispatch_connection_id=UUID(CONNECTION_ID),
                    dispatch_message_id=UUID(
                        "66666666-6666-4666-8666-666666666662"
                    ),
                    dispatch_sequence=1,
                    dispatch_attempts=1,
                    received_at=NOW,
                    updated_at=NOW,
                    receipt_sent_at=NOW,
                    receipt_settled_at=None,
                    retention_until=NOW + timedelta(days=7),
                )
            )
        await authority.disconnect_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
        )

        resolution = await authority.resolve(
            identity,
            ConnectorResumePosition(
                "resume",
                CONNECTION_ID,
                2,
                resume_next_inbound,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert resolution.decision == expected_decision
        with factory() as session:
            receipt = session.get(
                SessionCatalogInboxModel,
                (UUID(TENANT_ID), catalog_message_id),
            )
            assert receipt is not None
            assert receipt.receipt_state == expected_state
        engine.dispose()

    asyncio.run(scenario())


def test_prepare_before_send_crash_recovers_only_after_bounded_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, _factory, authority = _seed_authority(
            tmp_path / "prepare-crash.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await authority.prepare_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            resume_decision="fresh",
            handshake_disposition="advance",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )

        restarted = SqlAlchemyConnectorTransportCursorAuthority(
            _factory,
            now=clock,
            ownership_lease_seconds=90.0,
        )
        with pytest.raises(RuntimeError, match="handshake ownership"):
            await restarted.resolve(
                identity,
                ConnectorResumePosition("fresh", None, 0, 0),
                connector_instance_id=CONNECTOR_ID,
                runtime_generation=RUNTIME_GENERATION,
            )
        clock.advance(91)
        resolution = await restarted.resolve(
            identity,
            ConnectorResumePosition("fresh", None, 0, 0),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert resolution.decision == "fresh"
        assert resolution.handshake_disposition == "advance"
        assert (resolution.next_connector_sequence, resolution.next_cloud_sequence) == (
            0,
            0,
        )
        engine.dispose()

    asyncio.run(scenario())


def test_send_before_confirm_crash_recovers_from_exact_welcome_proof(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, factory, authority = _seed_authority(
            tmp_path / "sent-crash.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await authority.prepare_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            resume_decision="fresh",
            handshake_disposition="advance",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )

        restarted = SqlAlchemyConnectorTransportCursorAuthority(
            factory,
            now=clock,
            ownership_lease_seconds=90.0,
        )
        resolution = await restarted.resolve(
            identity,
            ConnectorResumePosition("resume", CONNECTION_ID, 1, 1),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )

        assert resolution.decision == "resumed"
        assert resolution.handshake_disposition == "advance"
        assert (resolution.next_connector_sequence, resolution.next_cloud_sequence) == (
            1,
            1,
        )
        with factory() as session:
            cursor = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert cursor is not None
            assert cursor.state == "offline"
            assert (cursor.next_connector_sequence, cursor.next_cloud_sequence) == (
                1,
                1,
            )
            assert (
                session.get(
                    ConnectorTransportHandshakeOwnershipModel,
                    (UUID(TENANT_ID), UUID(DEVICE_ID)),
                )
                is None
            )
        engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "proof_case",
    (
        "exact-ack",
        "exact-nack",
        "not-sent",
        "wrong-tenant",
        "wrong-device",
        "wrong-previous-connection",
        "cloud-gap",
        "connector-gap",
        "receipt-identity-mismatch",
        "ambiguous",
        "hello-outbound-behind",
        "hello-outbound-ahead",
        "hello-inbound-behind",
        "hello-inbound-ahead",
    ),
)
def test_sent_observer_receipt_proof_recovers_the_uncommitted_cursor_pair(
    tmp_path: Path,
    proof_case: str,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, factory, authority = _seed_authority(
            tmp_path / "sent-observer-receipt.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await _prepare_and_confirm(
            authority,
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            resume_decision="fresh",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        await authority.commit_cursors(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            expected_next_connector_sequence=1,
            expected_next_cloud_sequence=1,
            next_connector_sequence=2,
            next_cloud_sequence=2,
        )
        observer_message_id = UUID("66666666-6666-4666-8666-666666666666")
        proof_tenant_id = UUID(TENANT_ID)
        proof_device_id = UUID(DEVICE_ID)
        if proof_case == "wrong-tenant":
            proof_tenant_id = UUID("88888888-8888-4888-8888-888888888888")
            proof_device_id = UUID("99999999-9999-4999-8999-999999999999")
        elif proof_case == "wrong-device":
            proof_device_id = UUID("99999999-9999-4999-8999-999999999999")
        dispatch_connection_id = (
            UUID(REPLACEMENT_ID)
            if proof_case == "wrong-previous-connection"
            else UUID(CONNECTION_ID)
        )
        dispatch_sequence = 3 if proof_case == "cloud-gap" else 2
        connector_sequence = 3 if proof_case == "connector-gap" else 2
        sent_at = None if proof_case == "not-sent" else NOW
        receipt_type = "stream.nack" if proof_case == "exact-nack" else "stream.ack"
        payload_observer_id = (
            UUID(REPLACEMENT_ID)
            if proof_case == "receipt-identity-mismatch"
            else observer_message_id
        )
        with factory.begin() as session:
            if proof_case == "wrong-tenant":
                session.add(
                    TenantModel(
                        tenant_id=proof_tenant_id,
                        slug="transport-proof-other-tenant",
                        display_name="Transport Proof Other Tenant",
                        status="active",
                        created_at=NOW,
                    )
                )
                session.flush()
            if proof_case in {"wrong-tenant", "wrong-device"}:
                session.add(
                    DeviceModel(
                        tenant_id=proof_tenant_id,
                        device_id=proof_device_id,
                        agent_id=None,
                        workspace_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                        device_key=f"transport-proof-{proof_case}",
                        status="active",
                        created_at=NOW,
                    )
                )
                session.flush()
            session.add(
                ConnectorObserverReceiptModel(
                    tenant_id=proof_tenant_id,
                    device_id=proof_device_id,
                    observer_message_id=observer_message_id,
                    receipt_type=receipt_type,
                    payload={
                        "observer_message_id": str(payload_observer_id),
                        "connector_sequence": connector_sequence,
                    },
                    payload_digest="a" * 64,
                    state="pending",
                    dispatch_connection_id=dispatch_connection_id,
                    dispatch_message_id=UUID("77777777-7777-4777-8777-777777777777"),
                    dispatch_sequence=dispatch_sequence,
                    dispatch_attempts=1,
                    created_at=NOW,
                    updated_at=NOW,
                    sent_at=sent_at,
                    settled_at=None,
                )
            )
            if proof_case == "ambiguous":
                second_message_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab")
                session.add(
                    ConnectorObserverReceiptModel(
                        tenant_id=UUID(TENANT_ID),
                        device_id=UUID(DEVICE_ID),
                        observer_message_id=second_message_id,
                        receipt_type="stream.ack",
                        payload={
                            "observer_message_id": str(second_message_id),
                            "connector_sequence": 2,
                        },
                        payload_digest="b" * 64,
                        state="pending",
                        dispatch_connection_id=UUID(CONNECTION_ID),
                        dispatch_message_id=UUID(
                            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac"
                        ),
                        dispatch_sequence=2,
                        dispatch_attempts=1,
                        created_at=NOW,
                        updated_at=NOW,
                        sent_at=NOW,
                        settled_at=None,
                    )
                )
        await authority.disconnect_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
        )

        hello_pair = {
            "hello-outbound-behind": (2, 3),
            "hello-outbound-ahead": (4, 3),
            "hello-inbound-behind": (3, 2),
            "hello-inbound-ahead": (3, 4),
        }.get(proof_case, (3, 3))
        resolution = await authority.resolve(
            identity,
            ConnectorResumePosition("resume", CONNECTION_ID, *hello_pair),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )

        expected_decision = (
            "resumed" if proof_case in {"exact-ack", "exact-nack"} else "reset_required"
        )
        assert resolution.decision == expected_decision
        expected_position = (3, 3) if expected_decision == "resumed" else (2, 2)
        expected_disposition = (
            "advance" if expected_decision == "resumed" else "preserve"
        )
        assert resolution.handshake_disposition == expected_disposition
        assert (resolution.next_connector_sequence, resolution.next_cloud_sequence) == (
            expected_position
        )
        with factory() as session:
            cursor = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert cursor is not None
            assert cursor.state == "offline"
            assert (cursor.next_connector_sequence, cursor.next_cloud_sequence) == (
                expected_position
            )
        engine.dispose()

    asyncio.run(scenario())


def test_sent_welcome_proof_adopts_floor_then_resets_unconfirmed_outbound_tail(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, _factory, authority = _seed_authority(
            tmp_path / "sent-tail-crash.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await authority.prepare_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            resume_decision="fresh",
            handshake_disposition="advance",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )

        resolution = await authority.resolve(
            identity,
            ConnectorResumePosition("resume", CONNECTION_ID, 4, 1),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )

        assert resolution.decision == "reset_required"
        assert resolution.handshake_disposition == "preserve"
        assert (resolution.next_connector_sequence, resolution.next_cloud_sequence) == (
            1,
            1,
        )
        engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("next_outbound_sequence", "next_inbound_sequence"),
    ((0, 1), (1, 2)),
    ids=("outbound-below-floor", "inbound-above-confirmed-welcome"),
)
def test_sent_welcome_proof_rejects_an_invalid_cursor_pair(
    tmp_path: Path,
    next_outbound_sequence: int,
    next_inbound_sequence: int,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, factory, authority = _seed_authority(
            tmp_path
            / f"invalid-proof-{next_outbound_sequence}-{next_inbound_sequence}.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await authority.prepare_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            resume_decision="fresh",
            handshake_disposition="advance",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )

        with pytest.raises(RuntimeError, match="handshake ownership"):
            await authority.resolve(
                identity,
                ConnectorResumePosition(
                    "resume",
                    CONNECTION_ID,
                    next_outbound_sequence,
                    next_inbound_sequence,
                ),
                connector_instance_id=CONNECTOR_ID,
                runtime_generation=RUNTIME_GENERATION,
            )

        with factory() as session:
            assert (
                session.get(
                    ConnectorTransportCursorModel,
                    (UUID(TENANT_ID), UUID(DEVICE_ID)),
                )
                is None
            )
        engine.dispose()

    asyncio.run(scenario())


def test_active_lease_blocks_takeover_then_recovers_without_cursor_regression(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        engine, _factory, authority = _seed_authority(
            tmp_path / "active-crash.sqlite3",
            clock=clock,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await authority.prepare_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
            resume_decision="fresh",
            handshake_disposition="advance",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        await authority.confirm_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )

        with pytest.raises(RuntimeError, match="active ownership"):
            await authority.resolve(
                identity,
                ConnectorResumePosition("resume", CONNECTION_ID, 1, 1),
                connector_instance_id=CONNECTOR_ID,
                runtime_generation=RUNTIME_GENERATION,
            )
        clock.advance(91)
        resolution = await authority.resolve(
            identity,
            ConnectorResumePosition("resume", CONNECTION_ID, 1, 1),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert resolution.decision == "resumed"
        assert (resolution.next_connector_sequence, resolution.next_cloud_sequence) == (
            1,
            1,
        )
        engine.dispose()

    asyncio.run(scenario())


def test_authority_resumes_only_the_exact_durable_transport_position(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = build_sqlite_engine(
            _database_url(tmp_path / "transport.sqlite3"),
            allow_missing=True,
        )
        build_sqlite_metadata().create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory.begin() as session:
            session.add(
                TenantModel(
                    tenant_id=UUID(TENANT_ID),
                    slug="transport-test",
                    display_name="Transport Test",
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
                    device_key="transport-test-device",
                    status="active",
                    created_at=NOW,
                )
            )
        authority = SqlAlchemyConnectorTransportCursorAuthority(
            factory,
            now=lambda: NOW,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)

        await _prepare_and_confirm(
            authority,
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            resume_decision="fresh",
            previous_connection_id=None,
            expected_next_connector_sequence=0,
            expected_next_cloud_sequence=0,
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        for previous, following in (
            ((1, 1), (2, 2)),
            ((2, 2), (3, 3)),
            ((3, 3), (4, 4)),
            ((4, 4), (4, 5)),
        ):
            await authority.commit_cursors(
                identity=identity,
                connection_id=CONNECTION_ID,
                connector_instance_id=CONNECTOR_ID,
                runtime_generation=RUNTIME_GENERATION,
                expected_next_connector_sequence=previous[0],
                expected_next_cloud_sequence=previous[1],
                next_connector_sequence=following[0],
                next_cloud_sequence=following[1],
            )
        await authority.disconnect_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
        )
        resolution = await authority.resolve(
            identity,
            ConnectorResumePosition(
                mode="resume",
                previous_connection_id=CONNECTION_ID,
                next_outbound_sequence=4,
                next_inbound_sequence=5,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert resolution.decision == "resumed"
        assert resolution.handshake_disposition == "advance"
        assert resolution.next_connector_sequence == 4
        assert resolution.next_cloud_sequence == 5

        wrong_instance = await authority.resolve(
            identity,
            ConnectorResumePosition(
                mode="resume",
                previous_connection_id=CONNECTION_ID,
                next_outbound_sequence=4,
                next_inbound_sequence=5,
            ),
            connector_instance_id="66666666-6666-4666-8666-666666666666",
            runtime_generation=RUNTIME_GENERATION,
        )
        assert wrong_instance.decision == "fresh"
        assert wrong_instance.handshake_disposition == "preserve"
        assert wrong_instance.next_connector_sequence == 0
        assert wrong_instance.next_cloud_sequence == 0
        wrong_generation = await authority.resolve(
            identity,
            ConnectorResumePosition(
                mode="resume",
                previous_connection_id=CONNECTION_ID,
                next_outbound_sequence=4,
                next_inbound_sequence=5,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-2",
        )
        assert wrong_generation.decision == "fresh"
        assert wrong_generation.handshake_disposition == "preserve"
        assert wrong_generation.next_connector_sequence == 0
        assert wrong_generation.next_cloud_sequence == 0
        wrong_cursor = await authority.resolve(
            identity,
            ConnectorResumePosition(
                mode="resume",
                previous_connection_id=CONNECTION_ID,
                next_outbound_sequence=3,
                next_inbound_sequence=5,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert wrong_cursor.decision == "reset_required"
        assert wrong_cursor.handshake_disposition == "preserve"
        assert wrong_cursor.next_connector_sequence == 4
        assert wrong_cursor.next_cloud_sequence == 5
        same_epoch_fresh = await authority.resolve(
            identity,
            ConnectorResumePosition(
                mode="fresh",
                previous_connection_id=None,
                next_outbound_sequence=3,
                next_inbound_sequence=4,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert same_epoch_fresh.decision == "reset_required"
        assert same_epoch_fresh.handshake_disposition == "preserve"
        assert same_epoch_fresh.next_connector_sequence == 4
        assert same_epoch_fresh.next_cloud_sequence == 5
        wrong_device = await authority.resolve(
            ConnectorIdentity(
                TENANT_ID,
                "88888888-8888-4888-8888-888888888888",
            ),
            ConnectorResumePosition(
                mode="resume",
                previous_connection_id=CONNECTION_ID,
                next_outbound_sequence=4,
                next_inbound_sequence=5,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert wrong_device.decision == "fresh"
        wrong_device_fresh = await authority.resolve(
            ConnectorIdentity(
                TENANT_ID,
                "88888888-8888-4888-8888-888888888888",
            ),
            ConnectorResumePosition(
                mode="fresh",
                previous_connection_id=None,
                next_outbound_sequence=9,
                next_inbound_sequence=13,
            ),
            connector_instance_id=CONNECTOR_ID,
            runtime_generation=RUNTIME_GENERATION,
        )
        assert wrong_device_fresh.decision == "fresh"
        assert wrong_device_fresh.handshake_disposition == "preserve"
        assert wrong_device_fresh.next_connector_sequence == 0
        assert wrong_device_fresh.next_cloud_sequence == 0

        await _prepare_and_confirm(
            authority,
            identity=identity,
            connection_id=REPLACEMENT_ID,
            connector_instance_id=CONNECTOR_ID,
            resume_decision="resumed",
            previous_connection_id=CONNECTION_ID,
            expected_next_connector_sequence=4,
            expected_next_cloud_sequence=5,
            next_connector_sequence=5,
            next_cloud_sequence=6,
        )
        await authority.disconnect_session(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
        )
        with pytest.raises(RuntimeError, match="ownership changed"):
            await authority.commit_cursors(
                identity=identity,
                connection_id=CONNECTION_ID,
                connector_instance_id=CONNECTOR_ID,
                runtime_generation=RUNTIME_GENERATION,
                expected_next_connector_sequence=5,
                expected_next_cloud_sequence=6,
                next_connector_sequence=6,
                next_cloud_sequence=6,
            )
        with factory() as session:
            row = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert row is not None
            assert row.connection_id == UUID(REPLACEMENT_ID)
            assert row.state == "active"
            assert row.next_connector_sequence == 5
            assert row.next_cloud_sequence == 6
        engine.dispose()

    asyncio.run(scenario())


def test_two_concurrent_fresh_activations_have_one_atomic_owner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = build_sqlite_engine(
            _database_url(tmp_path / "concurrent-fresh.sqlite3"),
            allow_missing=True,
        )
        build_sqlite_metadata().create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory.begin() as session:
            session.add(
                TenantModel(
                    tenant_id=UUID(TENANT_ID),
                    slug="transport-concurrency-test",
                    display_name="Transport Concurrency Test",
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
                    device_key="transport-concurrency-device",
                    status="active",
                    created_at=NOW,
                )
            )

        read_barrier = threading.Barrier(2)

        def synchronize_empty_reads(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if (
                threading.current_thread().name != "MainThread"
                and statement.lstrip().upper().startswith("SELECT")
                and "connector_transport_handshake_ownerships" in statement
            ):
                read_barrier.wait(timeout=5)

        event.listen(engine, "before_cursor_execute", synchronize_empty_reads)
        authority = SqlAlchemyConnectorTransportCursorAuthority(
            factory,
            now=lambda: NOW,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)

        async def activate(connection_id: str, instance_id: str) -> None:
            await _prepare_and_confirm(
                authority,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=instance_id,
                resume_decision="fresh",
                previous_connection_id=None,
                expected_next_connector_sequence=0,
                expected_next_cloud_sequence=0,
                next_connector_sequence=1,
                next_cloud_sequence=1,
            )

        try:
            outcomes = await asyncio.gather(
                activate(
                    CONNECTION_ID,
                    CONNECTOR_ID,
                ),
                activate(
                    REPLACEMENT_ID,
                    "66666666-6666-4666-8666-666666666666",
                ),
                return_exceptions=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", synchronize_empty_reads)

        assert sum(outcome is None for outcome in outcomes) == 1
        conflicts = [outcome for outcome in outcomes if outcome is not None]
        assert len(conflicts) == 1
        assert isinstance(conflicts[0], RuntimeError)
        assert "ownership" in str(conflicts[0]).lower()
        with factory() as session:
            row = session.get(
                ConnectorTransportCursorModel,
                (UUID(TENANT_ID), UUID(DEVICE_ID)),
            )
            assert row is not None
            assert row.connection_id in {UUID(CONNECTION_ID), UUID(REPLACEMENT_ID)}
            assert row.state == "active"
        engine.dispose()

    asyncio.run(scenario())
