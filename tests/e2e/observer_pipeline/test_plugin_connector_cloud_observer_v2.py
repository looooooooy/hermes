"""Production Observer v2 pipeline with only the future Host SPI doubled."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.host.observer_v2 import ObserverV2Violation
from hermes_cloud.adapters.business_api_runtime import (
    build_production_business_api_application,
)
from hermes_cloud.application.connector_gateway import ConnectorGatewaySettings
from hermes_cloud.entrypoints.connector_gateway.bootstrap import (
    build_production_connector_gateway_application,
)
from hermes_cloud.platform.postgres.models import AgentModel, SessionProjectionModel
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverEventModel,
    ObserverInboxModel,
    ObserverSessionModel,
    ObserverV2StateModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.websocket_transport import WebSocketsCloudTransport
from hermes_connector.adapters.platform.macos.agent_discovery import (
    MacOSAgentDiscovery,
)
from hermes_connector.adapters.platform.macos.credentials import (
    MacOSFileCloudTokenProvider,
)
from hermes_connector.adapters.platform.macos.foundation_projection import (
    FoundationNoOpLocalProjectionInvalidator,
)
from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MacOSLocalGatewayTransport,
)
from hermes_connector.adapters.platform.macos.observer_client import MacOSObserverClient
from hermes_connector.adapters.platform.macos.observer_discovery import (
    MacOSObserverEndpointDiscovery,
)
from hermes_connector.application.cloud_wss_client import CloudClientConfig
from hermes_connector.application.local_gateway_client import LocalGatewayClient
from hermes_connector.application.observer_intent_lane import ObserverIntentLane
from hermes_connector.application.observer_outbound_lane import ObserverOutboundLane
from hermes_connector.bootstrap.cloud import build_cloud_wss_client
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.observer import (
    SessionEvent,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)
from hermes_connector.domain.storage import ObserverOutboxRecord
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect

from tests.e2e.connector_cloud_interop.test_real_wss_gateway import (
    _migrate_sqlite,
    _mint_connector_token,
    _seed_legacy_device_authority,
    _seed_test_server_base,
)
from tests.e2e.observer_pipeline import harness
from tests.e2e.observer_pipeline.test_plugin_connector_cloud_observer import (
    _post_json,
)

OUTPUT_PARITY_CAPABILITY = "session.observe.output-parity.v1"


def test_v2_host_snapshot_contains_all_lifecycle_collections_and_replay(
    tmp_path: Path,
) -> None:
    host = harness.GatewayExtensionV2TestHost(harness.local_paths(tmp_path))
    prepared = host.sessions[harness.SESSION_A].prepare(lambda _event: None)
    try:
        snapshot = dict(prepared.snapshot)
        assert snapshot["observer_contract"] == 2
        assert snapshot["snapshot_event_sequence"] == 4
        assert snapshot["event_sequence"] == 5
        assert [item["section_id"] for item in snapshot["todo_sections"]] == ["todo-1"]
        assert [item["subagent_id"] for item in snapshot["subagents"]] == ["subagent-1"]
        assert [item["tool_call_id"] for item in snapshot["tools"]] == ["tool-1"]
        assert [item["process_id"] for item in snapshot["terminals"]] == ["process-1"]
        assert [event["type"] for event in snapshot["replay_events"]] == ["todo.update"]
        assert snapshot["replay_events"][0]["event_sequence"] == 5
    finally:
        prepared.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AF_UNIX relay")
def test_v2_host_double_advertises_the_exact_output_parity_capability(
    tmp_path: Path,
) -> None:
    host_type = getattr(harness, "GatewayExtensionV2TestHost", None)

    assert host_type is not None, "Observer v2 Host double is not implemented"
    host = host_type(harness.local_paths(tmp_path))
    descriptor = host.runtime_descriptor()

    assert descriptor.capabilities == frozenset(
        {
            "approval.respond",
            "clarify.respond",
            "prompt.submit",
            "session.control",
            "session.interrupt",
            "session.observe",
            OUTPUT_PARITY_CAPABILITY,
            "session.steer",
        }
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AF_UNIX relay")
@pytest.mark.asyncio
async def test_v2_host_and_connector_negotiate_output_parity_over_real_uds(
    tmp_path: Path,
) -> None:
    harness.require_live_host_spi()
    paths = harness.local_paths(tmp_path)
    host = harness.GatewayExtensionV2TestHost(paths)
    config = ConnectorConfig(local_discovery_poll_interval_seconds=30.0)
    client = LocalGatewayClient(
        profile=harness.PROFILE,
        client_instance_id=UUID("92000000-0000-4000-8000-000000000002"),
        required_capabilities=("session.observe", OUTPUT_PARITY_CAPABILITY),
        optional_capabilities=(),
        discovery=MacOSAgentDiscovery(
            paths.local_gateway_registry_directory,
            paths.local_gateway_socket_directory,
        ),
        transport=MacOSLocalGatewayTransport(),
        session_state=FoundationNoOpLocalProjectionInvalidator(),
        config=config,
    )
    task: asyncio.Task[None] | None = None
    observer: MacOSObserverClient | None = None
    try:
        host.start()
        await client.start()
        task = asyncio.create_task(client.run(), name="e2e-v2-local-gateway")

        assert await asyncio.wait_for(client.ready(), timeout=3)
        authority = await client.current_runtime_authority()
        assert authority is not None
        assert authority.required_capabilities == (
            "session.observe",
            OUTPUT_PARITY_CAPABILITY,
        )
        observer = MacOSObserverClient(
            discovery=MacOSObserverEndpointDiscovery(
                paths.observer_registry_directory,
                paths.observer_socket_directory,
            ),
            authority=client.current_runtime_authority,
        )
        subscription = await observer.subscribe(
            profile=harness.PROFILE,
            session_key=harness.SESSION_A,
        )
        assert subscription.snapshot.observer_contract == 2
        await subscription.close()
    finally:
        if observer is not None:
            await observer.aclose()
        await client.drain()
        await client.stop()
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        host.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AF_UNIX relay")
@pytest.mark.asyncio
async def test_production_plugin_connector_cloud_business_observer_v2_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.require_live_host_spi()
    database_url = f"sqlite+pysqlite:///{tmp_path / 'cloud-v2.sqlite3'}"
    runtime_dsn = harness.private_file(tmp_path / "runtime-dsn", database_url)
    migration_dsn = harness.private_file(tmp_path / "migration-dsn", database_url)
    seed_tenant_id = UUID("10000000-0000-4000-8000-000000000001")
    observer_keyring = harness.keyring_file(
        tmp_path / "observer-keyring",
        seed_tenant_id,
    )
    monkeypatch.setenv("HERMES_OBSERVER_KEYRING_FILE", str(observer_keyring))
    await asyncio.to_thread(_migrate_sqlite, migration_dsn)
    password = "observer-v2-e2e-password"
    await asyncio.to_thread(
        _seed_test_server_base,
        runtime_dsn,
        harness.private_file(tmp_path / "initial-password", password),
    )
    tenant_id, device_id = await asyncio.to_thread(
        _seed_legacy_device_authority,
        database_url,
    )
    harness.keyring_file(observer_keyring, tenant_id)
    connector_secret = harness.private_file(
        tmp_path / "connector-signing-secret",
        "c" * 48,
    )
    connector_token = tmp_path / "connector-token"
    await asyncio.to_thread(
        _mint_connector_token,
        connector_secret,
        connector_token,
        tenant_id=tenant_id,
        device_id=device_id,
    )
    business_secret = harness.private_file(
        tmp_path / "business-signing-secret",
        "b" * 48,
    )
    cloud_engine = build_sqlite_engine(database_url)
    with Session(cloud_engine) as session:
        agent = session.scalars(
            select(AgentModel).where(AgentModel.tenant_id == tenant_id)
        ).one()
        seeded_session = session.scalars(
            select(SessionProjectionModel).where(
                SessionProjectionModel.tenant_id == tenant_id,
                SessionProjectionModel.session_key == harness.SESSION_A,
            )
        ).one()
        agent_id = agent.agent_id
        now = datetime.now(UTC)
        session.add(
            SessionProjectionModel(
                tenant_id=tenant_id,
                session_id=UUID(harness.RUNTIME_SESSION_B),
                session_key=harness.SESSION_B,
                workspace_id=seeded_session.workspace_id,
                agent_id=agent_id,
                profile=harness.PROFILE,
                title="Second Observer v2 Session",
                state="active",
                revision=1,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=now,
                updated_at=now,
                closed_at=None,
                retention_until=now + timedelta(days=30),
            )
        )
        session.commit()

    paths = harness.local_paths(tmp_path)
    host = harness.GatewayExtensionV2TestHost(paths)
    baseline_tasks = frozenset(harness.live_noncurrent_tasks())
    baseline_threads = harness.live_thread_names()
    baseline_fds = harness.open_fd_count()
    gateway_server: harness.RunningAsgiServer | None = None
    business_server: harness.RunningAsgiServer | None = None
    storage = None
    storage_task: asyncio.Task[None] | None = None
    local_gateway: LocalGatewayClient | None = None
    local_task: asyncio.Task[None] | None = None
    cloud_client = None
    cloud_task: asyncio.Task[None] | None = None
    allow_replacement_ack = asyncio.Event()
    primary_websocket = None
    keepalive_websocket = None
    recovery_websocket = None
    jwt_websocket = None
    try:
        host.start()
        environment = {
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(connector_secret),
            "HERMES_OBSERVER_KEYRING_FILE": str(observer_keyring),
            "HERMES_RUNTIME_DSN_FILE": str(runtime_dsn),
        }
        gateway = build_production_connector_gateway_application(
            environment=environment,
            settings=ConnectorGatewaySettings(heartbeat_interval_ms=20_000),
        )
        business = build_production_business_api_application(
            environment={
                **environment,
                "HERMES_SIGNING_SECRET_FILE": str(business_secret),
            }
        )
        gateway_server = harness.RunningAsgiServer(gateway)
        business_server = harness.RunningAsgiServer(business)
        gateway_port = await gateway_server.start()
        business_port = await business_server.start()

        config = ConnectorConfig(local_discovery_poll_interval_seconds=30.0)
        from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent

        storage = SQLiteStorageComponent(tmp_path / "connector-v2.sqlite3", config)
        await storage.start()
        storage_task = asyncio.create_task(storage.run(), name="e2e-v2-storage")
        assert await asyncio.wait_for(storage.ready(), timeout=3)

        local_gateway = LocalGatewayClient(
            profile=harness.PROFILE,
            client_instance_id=UUID("92000000-0000-4000-8000-000000000003"),
            required_capabilities=("session.observe", OUTPUT_PARITY_CAPABILITY),
            optional_capabilities=(),
            discovery=MacOSAgentDiscovery(
                paths.local_gateway_registry_directory,
                paths.local_gateway_socket_directory,
            ),
            transport=MacOSLocalGatewayTransport(),
            session_state=FoundationNoOpLocalProjectionInvalidator(),
            config=config,
        )
        await local_gateway.start()
        local_task = asyncio.create_task(
            local_gateway.run(),
            name="e2e-v2-local-gateway",
        )
        assert await asyncio.wait_for(local_gateway.ready(), timeout=3)

        codec = ConnectorProtocolCodec()
        inbound_message_types: list[str] = []
        production_decode_envelope = codec.decode_envelope

        def record_inbound_envelope(raw: object):
            envelope = production_decode_envelope(raw)
            inbound_message_types.append(envelope.message_type)
            return envelope

        codec.decode_envelope = record_inbound_envelope  # type: ignore[method-assign]
        observer_client = MacOSObserverClient(
            discovery=MacOSObserverEndpointDiscovery(
                paths.observer_registry_directory,
                paths.observer_socket_directory,
            ),
            authority=local_gateway.current_runtime_authority,
        )
        outbound = ObserverOutboundLane(
            storage=storage,
            codec=codec,
            tenant_id=str(tenant_id),
            device_id=str(device_id),
        )
        cloud_client = build_cloud_wss_client(
            config=CloudClientConfig(
                endpoint=f"ws://127.0.0.1:{gateway_port}/api/ws",
                tenant_id=str(tenant_id),
                device_id=str(device_id),
                connector_instance_id=UUID("91000000-0000-4000-8000-000000000002"),
                connector_version="1.0.0",
                negotiation_timeout_seconds=3,
                io_timeout_seconds=3,
            ),
            token_provider=MacOSFileCloudTokenProvider(connector_token),
            storage=storage,
            runtime_authority=local_gateway,
            observer_outbound_lane=outbound,
            transport=WebSocketsCloudTransport(),
            codec=codec,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        snapshots: list[ObserverOutboxRecord] = []
        events: list[ObserverOutboxRecord] = []
        unexpected_publish_before_ack = asyncio.Event()
        production_snapshot = cloud_client.publish_observer_snapshot
        production_event = cloud_client.publish_observer_event

        async def record_snapshot(
            snapshot: SessionSnapshot,
            *,
            force_new_attempt: bool = False,
        ) -> ObserverOutboxRecord:
            record = await production_snapshot(
                snapshot,
                force_new_attempt=force_new_attempt,
            )
            snapshots.append(record)
            return record

        async def record_event(event: SessionEvent) -> ObserverOutboxRecord:
            if replacement_ack_arrived.is_set() and not allow_replacement_ack.is_set():
                unexpected_publish_before_ack.set()
            record = await production_event(event)
            events.append(record)
            return record

        cloud_client.publish_observer_snapshot = record_snapshot  # type: ignore[method-assign]
        cloud_client.publish_observer_event = record_event  # type: ignore[method-assign]
        intents = ObserverIntentLane(
            local_client=observer_client, publisher=cloud_client
        )
        opened: list[object] = []
        acks: list[StreamAck] = []
        nacks: list[StreamNack] = []
        recovery_phase = asyncio.Event()
        replacement_ack_arrived = asyncio.Event()
        production_open = intents.open
        production_ack = intents.acknowledge
        production_recover = intents.recover

        async def record_open(intent: object) -> None:
            opened.append(intent)
            await production_open(intent)

        async def record_ack(ack: StreamAck) -> None:
            acks.append(ack)
            if (
                recovery_phase.is_set()
                and ack.observer_message_type == "session.snapshot.v2"
            ):
                replacement_ack_arrived.set()
                await allow_replacement_ack.wait()
            await production_ack(ack)

        async def record_nack(nack: StreamNack) -> None:
            nacks.append(nack)
            await production_recover(nack)

        intents.open = record_open  # type: ignore[method-assign]
        intents.acknowledge = record_ack  # type: ignore[method-assign]
        intents.recover = record_nack  # type: ignore[method-assign]
        cloud_client.bind_observer_intent_lane(intents)
        await cloud_client.start()
        cloud_task = asyncio.create_task(cloud_client.run(), name="e2e-v2-cloud")

        status, _body, set_cookies = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/auth/password-login",
            {
                "provider": "basic",
                "username": "interop@example.test",
                "password": password,
                "next": "",
            },
        )
        assert status == 200
        from http.cookies import SimpleCookie

        cookies = SimpleCookie()
        for value in set_cookies:
            cookies.load(value)
        client_instance_id = "93000000-0000-4000-8000-000000000001"
        ticket_status, ticket_body, _ = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
            {
                "connection_role": "observer",
                "client_instance_id": client_instance_id,
                "observer_contract": 2,
            },
            authorization=f"Bearer {cookies['hermes_session_at'].value}",
        )
        assert ticket_status == 200
        assert set(ticket_body) == {
            "ticket",
            "ttl_seconds",
            "connection_role",
            "observer_contract",
        }
        assert ticket_body["observer_contract"] == 2

        primary_websocket = await connect(
            f"ws://127.0.0.1:{business_port}/api/ws?ticket={ticket_body['ticket']}",
            subprotocols=("hermes.tui.v2",),
            open_timeout=3,
            close_timeout=3,
            ping_interval=None,
        )
        websocket = primary_websocket
        async with asyncio.timeout(10):
            assert websocket.subprotocol == "hermes.tui.v2"
            ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
            assert ready == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "observer_contract": 2,
                        "connection_role": "observer",
                    },
                },
            }
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "session.observe.subscribe",
                        "params": {
                            "observer_contract": 2,
                            "session_key": harness.SESSION_A,
                            "profile": harness.PROFILE,
                        },
                    },
                    separators=(",", ":"),
                )
            )
            response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            result = response["result"]
            assert response["id"] == 1
            assert result["observer_contract"] == 2
            assert result["snapshot_event_sequence"] == 4
            assert result["event_sequence"] == 5
            assert len(result["todo_sections"]) == 1
            assert len(result["subagents"]) == 1
            assert len(result["tools"]) == 1
            assert len(result["terminals"]) == 1
            assert [item["event_sequence"] for item in result["replay_events"]] == [5]

            live_sequence = host.sessions[harness.SESSION_A].emit(
                event_type="tool.update",
                payload={
                    "turn_id": "turn-1",
                    "tool_call_id": "tool-1",
                    "revision": 2,
                    "first_event_sequence": 3,
                    "operation": "upsert",
                    "status": "completed",
                    "name": "E2E tests",
                },
            )
            assert live_sequence == 6
            live = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            assert live["params"]["observer_contract"] == 2
            assert live["params"]["type"] == "tool.update"
            assert live["params"]["event_sequence"] == 6

        keepalive_status, keepalive_ticket, _ = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
            {
                "connection_role": "observer",
                "client_instance_id": "93000000-0000-4000-8000-000000000002",
                "observer_contract": 2,
            },
            authorization=f"Bearer {cookies['hermes_session_at'].value}",
        )
        assert keepalive_status == 200
        keepalive_websocket = await connect(
            "ws://127.0.0.1:"
            f"{business_port}/api/ws?ticket={keepalive_ticket['ticket']}",
            subprotocols=("hermes.tui.v2",),
            open_timeout=3,
            close_timeout=3,
            ping_interval=None,
        )
        await asyncio.wait_for(keepalive_websocket.recv(), timeout=3)
        await keepalive_websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.observe.subscribe",
                    "params": {
                        "observer_contract": 2,
                        "session_key": harness.SESSION_A,
                        "profile": harness.PROFILE,
                    },
                },
                separators=(",", ":"),
            )
        )
        keepalive_snapshot = json.loads(
            await asyncio.wait_for(keepalive_websocket.recv(), timeout=10)
        )
        assert keepalive_snapshot["result"]["observer_contract"] == 2
        assert keepalive_snapshot["result"]["event_sequence"] == 6
        await primary_websocket.close()
        primary_websocket = None

        async with asyncio.timeout(10):
            while len(acks) < 2:
                await asyncio.sleep(0.05)
        assert inbound_message_types.count("session.observe.open.v2") == 1
        assert snapshots[0].message_type == "session.snapshot.v2"
        assert events[0].message_type == "session.event.v2"
        assert all(ack.observer_contract == 2 for ack in acks)
        assert {ack.observer_message_type for ack in acks} >= {
            "session.snapshot.v2",
            "session.event.v2",
        }
        assert nacks == []
        with Session(cloud_engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(ObserverInboxModel))
                == 2
            )
            assert (
                session.scalar(select(func.count()).select_from(ObserverSessionModel))
                == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(ObserverV2StateModel))
                == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(ObserverEventModel))
                == 2
            )
            projected = session.scalars(
                select(ObserverSessionModel).where(
                    ObserverSessionModel.tenant_id == tenant_id,
                    ObserverSessionModel.agent_id == agent_id,
                    ObserverSessionModel.session_key == harness.SESSION_A,
                )
            ).one()
            assert projected.event_sequence == 6

        with Session(cloud_engine) as session:
            projected = session.scalars(
                select(ObserverSessionModel).where(
                    ObserverSessionModel.tenant_id == tenant_id,
                    ObserverSessionModel.agent_id == agent_id,
                    ObserverSessionModel.session_key == harness.SESSION_A,
                )
            ).one()
            projected.event_sequence = 5
            session.query(ObserverEventModel).filter(
                ObserverEventModel.tenant_id == tenant_id,
                ObserverEventModel.session_id == projected.session_id,
                ObserverEventModel.event_sequence == 6,
            ).delete()
            session.commit()

        recovery_phase.set()
        gap_sequence = host.sessions[harness.SESSION_A].emit(
            event_type="terminal.update",
            payload={
                "turn_id": "turn-1",
                "process_id": "process-1",
                "revision": 2,
                "first_event_sequence": 4,
                "operation": "upsert",
                "status": "completed",
                "exit_code": 0,
            },
        )
        assert gap_sequence == 7
        async with asyncio.timeout(10):
            while len(nacks) < 1 or len(snapshots) < 2:
                await asyncio.sleep(0.05)
        assert nacks[-1].observer_contract == 2
        assert nacks[-1].observer_message_type == "session.event.v2"
        assert nacks[-1].reason == "event_gap"
        assert nacks[-1].expected_event_sequence == 6
        assert nacks[-1].recovery == "send_snapshot"
        assert snapshots[-1].message_type == "session.snapshot.v2"
        assert snapshots[-1].event_sequence == 7

        await asyncio.wait_for(replacement_ack_arrived.wait(), timeout=10)
        rejected_record = await storage.get_observer_outbox(events[-1].message_id)
        replacement_record = await storage.get_observer_outbox(snapshots[-1].message_id)
        assert rejected_record is not None
        assert rejected_record.state == "rejected"
        assert replacement_record is not None
        assert replacement_record.state == "acked"

        published_before_barrier = len(events)
        barrier_sequence = host.sessions[harness.SESSION_A].emit(
            event_type="subagent.update",
            payload={
                "turn_id": "turn-1",
                "subagent_id": "subagent-1",
                "revision": 2,
                "first_event_sequence": 2,
                "operation": "upsert",
                "parent_subagent_id": None,
                "name": "Pipeline verifier",
                "goal": "Verify the production observer path",
                "summary": "Verified through the production pipeline",
                "status": "completed",
            },
        )
        assert barrier_sequence == 8
        await asyncio.sleep(0.2)
        assert len(events) == published_before_barrier
        assert not unexpected_publish_before_ack.is_set()

        allow_replacement_ack.set()
        async with asyncio.timeout(10):
            while len(events) < published_before_barrier + 1 or len(acks) < 4:
                await asyncio.sleep(0.05)
        replacement_record = await storage.get_observer_outbox(snapshots[-1].message_id)
        barrier_record = await storage.get_observer_outbox(events[-1].message_id)
        assert replacement_record is not None
        assert replacement_record.state == "acked"
        assert barrier_record is not None
        assert barrier_record.message_type == "session.event.v2"
        assert barrier_record.event_sequence == 8
        assert barrier_record.state == "acked"

        recovery_status, recovery_ticket, _ = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
            {
                "connection_role": "observer",
                "client_instance_id": "93000000-0000-4000-8000-000000000003",
                "observer_contract": 2,
            },
            authorization=f"Bearer {cookies['hermes_session_at'].value}",
        )
        assert recovery_status == 200
        recovery_websocket = await connect(
            f"ws://127.0.0.1:{business_port}/api/ws?ticket={recovery_ticket['ticket']}",
            subprotocols=("hermes.tui.v2",),
            open_timeout=3,
            close_timeout=3,
            ping_interval=None,
        )
        recovery_ready = json.loads(
            await asyncio.wait_for(recovery_websocket.recv(), timeout=3)
        )
        assert recovery_ready["params"]["payload"] == {
            "observer_contract": 2,
            "connection_role": "observer",
        }
        await recovery_websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session.observe.subscribe",
                    "params": {
                        "observer_contract": 2,
                        "session_key": harness.SESSION_A,
                        "profile": harness.PROFILE,
                    },
                },
                separators=(",", ":"),
            )
        )
        recovery_response = json.loads(
            await asyncio.wait_for(recovery_websocket.recv(), timeout=10)
        )
        recovery_snapshot = recovery_response["result"]
        assert recovery_snapshot["observer_contract"] == 2
        assert recovery_snapshot["snapshot_event_sequence"] == 7
        assert recovery_snapshot["event_sequence"] == 8
        assert [
            event["event_sequence"] for event in recovery_snapshot["replay_events"]
        ] == [8]
        assert recovery_snapshot["replay_events"][0]["type"] == "subagent.update"
        assert len(recovery_snapshot["todo_sections"]) == 1
        assert len(recovery_snapshot["subagents"]) == 1
        assert len(recovery_snapshot["tools"]) == 1
        assert len(recovery_snapshot["terminals"]) == 1
        await keepalive_websocket.close()
        keepalive_websocket = None

        jwt_status, jwt_ticket, _ = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
            {
                "connection_role": "observer",
                "client_instance_id": "93000000-0000-4000-8000-000000000004",
                "observer_contract": 2,
            },
            authorization=f"Bearer {cookies['hermes_session_at'].value}",
        )
        assert jwt_status == 200
        jwt_websocket = await connect(
            f"ws://127.0.0.1:{business_port}/api/ws?ticket={jwt_ticket['ticket']}",
            subprotocols=("hermes.tui.v2",),
            open_timeout=3,
            close_timeout=3,
            ping_interval=None,
        )
        await asyncio.wait_for(jwt_websocket.recv(), timeout=3)
        await jwt_websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "session.observe.subscribe",
                    "params": {
                        "observer_contract": 2,
                        "session_key": harness.SESSION_B,
                        "profile": harness.PROFILE,
                    },
                },
                separators=(",", ":"),
            )
        )
        jwt_response = json.loads(
            await asyncio.wait_for(jwt_websocket.recv(), timeout=10)
        )
        assert "result" in jwt_response, jwt_response
        jwt_snapshot = jwt_response["result"]
        assert jwt_snapshot["observer_contract"] == 2
        assert jwt_snapshot["session_key"] == harness.SESSION_B
        assert await asyncio.to_thread(
            host.sessions[harness.SESSION_B].wait_for_active_count,
            1,
            3,
        )

        with Session(cloud_engine) as session:
            credential_counts_before = (
                session.scalar(select(func.count()).select_from(ObserverInboxModel)),
                session.scalar(select(func.count()).select_from(ObserverEventModel)),
            )
        basic_secret = "Authorization: Basic ZTJlLWJhc2ljOnByaXZhdGU="
        jwt_secret = (
            "eyJhbGciOiJFUzI1NiJ9."
            "eyJzdWIiOiJlMmUtand0LXByaXZhdGUifQ."
            "c2lnbmF0dXJlLXByaXZhdGUtMTIz"
        )
        outbox_before_credentials = (len(snapshots), len(events))
        with pytest.raises(ObserverV2Violation) as basic_rejected:
            host.sessions[harness.SESSION_A].inject_sensitive_extension(
                basic_secret,
            )
        with pytest.raises(ObserverV2Violation) as jwt_rejected:
            host.sessions[harness.SESSION_B].inject_sensitive_extension(jwt_secret)
        assert basic_secret not in str(basic_rejected.value)
        assert jwt_secret not in str(jwt_rejected.value)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(recovery_websocket.recv(), timeout=0.2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(jwt_websocket.recv(), timeout=0.2)
        await asyncio.sleep(0.1)
        assert (len(snapshots), len(events)) == outbox_before_credentials
        with Session(cloud_engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(ObserverInboxModel)),
                session.scalar(select(func.count()).select_from(ObserverEventModel)),
            ) == credential_counts_before
        for sqlite_file in tmp_path.glob("*sqlite3*"):
            assert basic_secret.encode() not in sqlite_file.read_bytes()
            assert jwt_secret.encode() not in sqlite_file.read_bytes()

        await recovery_websocket.close()
        recovery_websocket = None
        await jwt_websocket.close()
        jwt_websocket = None
        assert await asyncio.to_thread(
            host.sessions[harness.SESSION_A].wait_for_active_count,
            0,
            3,
        )
        assert await asyncio.to_thread(
            host.sessions[harness.SESSION_B].wait_for_active_count,
            0,
            3,
        )

    finally:
        allow_replacement_ack.set()
        for websocket in (
            jwt_websocket,
            recovery_websocket,
            keepalive_websocket,
            primary_websocket,
        ):
            if websocket is not None:
                await websocket.close()
        if cloud_client is not None:
            await cloud_client.stop()
        if cloud_task is not None:
            if not cloud_task.done():
                cloud_task.cancel()
            await asyncio.gather(cloud_task, return_exceptions=True)
        if local_gateway is not None:
            await local_gateway.drain()
            await local_gateway.stop()
        if local_task is not None:
            await asyncio.wait_for(local_task, timeout=5)
        if storage is not None:
            await storage.drain()
            await storage.stop()
        if storage_task is not None:
            await asyncio.wait_for(storage_task, timeout=5)
        if business_server is not None:
            await business_server.close()
        if gateway_server is not None:
            await gateway_server.close()
        host.close()
        cloud_engine.dispose()
        added_tasks = frozenset(harness.live_noncurrent_tasks()) - baseline_tasks
        assert not added_tasks, tuple(task.get_name() for task in added_tasks)
        added_threads = harness.live_thread_names() - baseline_threads
        assert all(
            name == "AnyIO worker thread" or name.startswith("asyncio_")
            for name in added_threads
        ), added_threads
        assert harness.open_fd_count() <= baseline_fds
