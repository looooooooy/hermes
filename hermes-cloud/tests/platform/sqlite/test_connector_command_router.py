from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.platform.postgres.models import (
    ConnectorBindingModel,
    ControlCommandModel,
)
from hermes_cloud.platform.sqlalchemy.connector_command_router import (
    SqlAlchemyConnectorCommandRouter,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEVICE_ID = "22222222-2222-4222-8222-222222222222"
CONNECTOR_ID = "33333333-3333-4333-8333-333333333333"
CONNECTION_ID = "44444444-4444-4444-8444-444444444444"
CLIENT_ID = "55555555-5555-4555-8555-555555555555"
COMMAND_ID = "66666666-6666-4666-8666-666666666666"
DELIVERY_ID = "77777777-7777-4777-8777-777777777777"


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _command() -> ControlCommandModel:
    return ControlCommandModel(
        tenant_id=TENANT_ID,
        command_id=COMMAND_ID,
        delivery_message_id=DELIVERY_ID,
        device_id=DEVICE_ID,
        connector_instance_id=CONNECTOR_ID,
        client_instance_id=CLIENT_ID,
        provider="basic",
        principal_id="88888888-8888-4888-8888-888888888888",
        session_key="android-bootstrap",
        profile="default",
        runtime_session_id="runtime-7",
        runtime_generation="runtime-generation-1",
        client_request_id="request-1",
        client_turn_id="turn-client-1",
        method="prompt.submit",
        params={
            "runtime_session_id": "runtime-7",
            "runtime_generation": "runtime-generation-1",
            "client_turn_id": "turn-client-1",
            "text": "Continue the current task.",
        },
        payload_digest="0" * 64,
        state="queued",
        revision=1,
        result=None,
        error=None,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        dispatch_connection_id=None,
        dispatch_sequence=None,
        delivery_sent_at=None,
        dispatched_at=None,
        receipt_message_id=None,
        receipt_digest=None,
        delivered_at=None,
        result_message_id=None,
        result_digest=None,
        completed_at=None,
        updated_at=NOW,
    )


def test_router_reserves_before_send_and_persists_strict_responses(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = build_sqlite_engine(
            _database_url(tmp_path / "router.sqlite3"),
            allow_missing=True,
        )
        build_sqlite_metadata().create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session, session.begin():
            session.add(_command())
        router = SqlAlchemyConnectorCommandRouter(
            factory,
            poll_interval_seconds=0.001,
            now=lambda: NOW,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        await router.connector_connected(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-1",
        )

        candidate = await asyncio.wait_for(
            router.wait_for_delivery(
                identity=identity,
                connection_id=CONNECTION_ID,
                connector_instance_id=CONNECTOR_ID,
                runtime_generation="runtime-generation-1",
            ),
            timeout=0.1,
        )
        assert candidate is not None
        reserved = await router.reserve_delivery(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            command_id=candidate.command_id,
            message_id=candidate.message_id,
            sequence=1,
        )
        assert reserved.sent_at == "2026-07-31T02:00:00.000Z"

        common = {
            "command_id": COMMAND_ID,
            "connector_instance_id": CONNECTOR_ID,
            "client_instance_id": CLIENT_ID,
            "session_key": "android-bootstrap",
            "profile": "default",
            "client_request_id": "request-1",
            "method": "prompt.submit",
        }
        await router.accept_connector_response(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-1",
            envelope=CloudEnvelope(
                contract_version=1,
                message_id="99999999-9999-4999-8999-999999999999",
                message_type="command.receipt",
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                sequence=1,
                sent_at="2026-07-31T02:00:01Z",
                payload={
                    **common,
                    "message_id": DELIVERY_ID,
                    "state": "delivered",
                    "stored_at": "2026-07-31T02:00:01Z",
                    "revision": 1,
                },
            ),
        )
        result_envelope = CloudEnvelope(
            contract_version=1,
            message_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            message_type="command.result",
            tenant_id=TENANT_ID,
            device_id=DEVICE_ID,
            sequence=2,
            sent_at="2026-07-31T02:00:02Z",
            payload={
                **common,
                "state": "succeeded",
                "completed_at": "2026-07-31T02:00:02Z",
                "revision": 2,
                "result": {
                    "status": "accepted",
                    "client_request_id": "request-1",
                    "client_turn_id": "turn-client-1",
                    "server_turn_id": "turn-server-1",
                },
            },
        )
        await router.accept_connector_response(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-1",
            envelope=result_envelope,
        )
        await router.accept_connector_response(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-1",
            envelope=result_envelope,
        )

        with factory() as session:
            row = session.get(ControlCommandModel, (TENANT_ID, COMMAND_ID))
            assert row is not None
            assert row.state == "succeeded"
            assert row.dispatch_connection_id == CONNECTION_ID
            assert row.dispatch_sequence == 1
            assert row.receipt_message_id == ("99999999-9999-4999-8999-999999999999")
            assert row.result_message_id == ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            assert row.result == result_envelope.payload["result"]
        engine.dispose()

    asyncio.run(scenario())


def test_old_connection_cannot_disconnect_or_mutate_replacement(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine = build_sqlite_engine(
            _database_url(tmp_path / "router.sqlite3"),
            allow_missing=True,
        )
        build_sqlite_metadata().create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        router = SqlAlchemyConnectorCommandRouter(
            factory,
            poll_interval_seconds=0.001,
            now=lambda: NOW,
        )
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)
        replacement = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        await router.connector_connected(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-1",
        )
        await router.connector_connected(
            identity=identity,
            connection_id=replacement,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-2",
        )
        await router.connector_disconnected(
            identity=identity,
            connection_id=CONNECTION_ID,
            connector_instance_id=CONNECTOR_ID,
        )

        with pytest.raises(RuntimeError, match="authoritative"):
            await router.connector_heartbeat(
                identity=identity,
                connection_id=CONNECTION_ID,
                connector_instance_id=CONNECTOR_ID,
                runtime_generation="runtime-generation-1",
                next_connector_sequence=2,
                next_cloud_sequence=2,
            )
        await router.connector_heartbeat(
            identity=identity,
            connection_id=replacement,
            connector_instance_id=CONNECTOR_ID,
            runtime_generation="runtime-generation-2",
            next_connector_sequence=3,
            next_cloud_sequence=4,
        )
        engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("termination", ("cancel", "timeout"))
def test_terminated_registration_compensates_late_orm_commit(
    tmp_path: Path,
    termination: str,
) -> None:
    async def scenario() -> None:
        engine = build_sqlite_engine(
            _database_url(tmp_path / "router.sqlite3"),
            allow_missing=True,
        )
        build_sqlite_metadata().create_all(engine)

        class BlockingSession(Session):
            pass

        factory = sessionmaker(
            bind=engine,
            class_=BlockingSession,
            expire_on_commit=False,
        )
        commit_started = Event()
        release_commit = Event()
        commit_lock = Lock()
        block_next_commit = True

        def before_commit(_session: Session) -> None:
            nonlocal block_next_commit
            with commit_lock:
                should_block = block_next_commit
                block_next_commit = False
            if should_block:
                commit_started.set()
                assert release_commit.wait(timeout=2)

        event.listen(BlockingSession, "before_commit", before_commit)
        router = SqlAlchemyConnectorCommandRouter(factory, now=lambda: NOW)
        identity = ConnectorIdentity(TENANT_ID, DEVICE_ID)

        async def connect() -> None:
            operation = router.connector_connected(
                identity=identity,
                connection_id=CONNECTION_ID,
                connector_instance_id=CONNECTOR_ID,
                runtime_generation="runtime-generation-1",
            )
            if termination == "timeout":
                await asyncio.wait_for(operation, timeout=0.05)
            else:
                await operation

        registration = asyncio.create_task(connect())
        try:
            assert await asyncio.to_thread(commit_started.wait, 1)
            if termination == "cancel":
                registration.cancel()
            expected_error = (
                asyncio.CancelledError if termination == "cancel" else TimeoutError
            )
            with pytest.raises(expected_error):
                await registration
            release_commit.set()

            state: str | None = None
            for _ in range(100):
                with factory() as session:
                    binding = session.get(
                        ConnectorBindingModel,
                        (TENANT_ID, DEVICE_ID),
                    )
                    state = None if binding is None else binding.state
                if state == "offline":
                    break
                await asyncio.sleep(0.005)
            assert state == "offline"
        finally:
            release_commit.set()
            event.remove(BlockingSession, "before_commit", before_commit)
            engine.dispose()

    asyncio.run(scenario())
