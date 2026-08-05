"""Real Observer snapshot vertical slice with only a Hermes Host SPI test double.

Passing this test does not mean Hermes 0.19 is integrated: that release lacks
gateway-extension/1. It proves the production Plugin/Connector/Cloud path on
the exact future Host contract implemented by ``GatewayExtensionV1TestHost``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import UUID

import pytest
from hermes_agent_plugin.ports import local_relay as plugin_local_relay
from hermes_cloud.adapters.business_api_runtime import (
    build_production_business_api_application,
)
from hermes_cloud.application.connector_gateway import ConnectorGatewaySettings
from hermes_cloud.entrypoints.connector_gateway.bootstrap import (
    build_production_connector_gateway_application,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    ConnectorObserverReceiptModel,
    ConnectorTransportCursorModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverEventModel,
    ObserverInboxModel,
    ObserverSessionModel,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverSubscriptionIntentModel,
    ObserverSubscriptionTargetModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.websocket_transport import (
    WebSocketsCloudTransport,
)
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
from hermes_connector.adapters.platform.macos.observer_client import (
    MacOSObserverClient,
)
from hermes_connector.adapters.platform.macos.observer_discovery import (
    MacOSObserverEndpointDiscovery,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.cloud_wss_client import CloudClientConfig
from hermes_connector.application.local_gateway_client import LocalGatewayClient
from hermes_connector.application.observer_intent_lane import ObserverIntentLane
from hermes_connector.application.observer_outbound_lane import ObserverOutboundLane
from hermes_connector.bootstrap.cloud import build_cloud_wss_client
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.observer import SessionEvent, SessionSnapshot, StreamAck
from hermes_connector.domain.storage import ObserverOutboxRecord
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect

from tests.e2e.connector_cloud_interop.test_real_wss_gateway import (
    _migrate_sqlite,
    _mint_connector_token,
    _seed_legacy_device_authority,
    _seed_test_server_base,
)
from tests.e2e.observer_pipeline.harness import (
    PROFILE,
    RUNTIME_GENERATION,
    RUNTIME_SESSION_A,
    RUNTIME_SESSION_B,
    SESSION_A,
    SESSION_B,
    GatewayExtensionV1TestHost,
    RunningAsgiServer,
    assert_canonical_uuid,
    keyring_file,
    live_noncurrent_tasks,
    live_thread_names,
    local_paths,
    open_fd_count,
    private_file,
    require_live_host_spi,
)


def _post_json(
    url: str,
    payload: dict[str, object],
    *,
    authorization: str | None = None,
) -> tuple[int, dict[str, object], tuple[str, ...]]:
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return (
            response.status,
            json.loads(response.read().decode("utf-8")),
            tuple(response.headers.get_all("Set-Cookie", ())),
        )


class _CommitCrashOnceAuthority:
    """Fail once after the Gateway send and before its real cursor commit."""

    def __init__(
        self,
        delegate: object,
        fault: str,
        *,
        failure_gate: asyncio.Event | None = None,
    ) -> None:
        self._delegate = delegate
        self._fault = fault
        self._failure_gate = failure_gate
        self._armed = fault == "subscription_intent"
        self.failed = asyncio.Event()
        self.reconnect_resolved = asyncio.Event()
        self.reset_required = asyncio.Event()
        self.target_committed = asyncio.Event()
        self.reconnect_decision: str | None = None

    def arm(self) -> None:
        self._armed = True

    async def resolve(self, *args: object, **kwargs: object):
        resolution = await self._delegate.resolve(*args, **kwargs)
        if self.failed.is_set():
            self.reconnect_decision = resolution.decision
            self.reconnect_resolved.set()
            if resolution.decision == "reset_required":
                self.reset_required.set()
        return resolution

    async def prepare_session(self, **kwargs: object) -> None:
        await self._delegate.prepare_session(**kwargs)

    async def confirm_session(self, **kwargs: object) -> None:
        await self._delegate.confirm_session(**kwargs)

    async def abort_session(self, **kwargs: object) -> None:
        await self._delegate.abort_session(**kwargs)

    async def disconnect_session(self, **kwargs: object) -> None:
        await self._delegate.disconnect_session(**kwargs)

    async def commit_cursors(self, **kwargs: object) -> None:
        connector_delta = int(kwargs["next_connector_sequence"]) - int(
            kwargs["expected_next_connector_sequence"]
        )
        cloud_delta = int(kwargs["next_cloud_sequence"]) - int(
            kwargs["expected_next_cloud_sequence"]
        )
        target = (
            (connector_delta, cloud_delta) == (1, 1)
            if self._fault == "observer_ack"
            else (connector_delta, cloud_delta) == (0, 1)
        )
        if target and self._armed and not self.failed.is_set():
            if self._failure_gate is not None:
                await self._failure_gate.wait()
            self.failed.set()
            raise RuntimeError("deterministic post-send cursor commit failure")
        await self._delegate.commit_cursors(**kwargs)
        if target:
            self.target_committed.set()


@pytest.fixture
def short_private_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="hmo-cloud-e2e-", dir="/tmp"))
    root.chmod(0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AF_UNIX relay")
def test_gateway_host_restores_the_previous_local_relay_backend_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_live_host_spi()
    previous_backend = object()
    monkeypatch.setattr(
        plugin_local_relay,
        "_backend_factory",
        lambda: previous_backend,
    )
    host = GatewayExtensionV1TestHost(local_paths(tmp_path))

    host.start()
    try:
        assert plugin_local_relay.get_local_relay_backend() is not previous_backend
    finally:
        host.close()

    assert plugin_local_relay.get_local_relay_backend() is previous_backend


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AF_UNIX relay")
@pytest.mark.parametrize(
    ("exercise_reconnect", "commit_crash"),
    (
        (False, None),
        (True, None),
        (False, "observer_ack"),
        (False, "subscription_intent"),
    ),
)
@pytest.mark.asyncio
async def test_business_subscribe_reaches_plugin_snapshot_cloud_ack_and_projection(
    short_private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    exercise_reconnect: bool,
    commit_crash: str | None,
) -> None:
    """Not real Hermes: exact Host SPI double, all downstream boundaries real."""

    require_live_host_spi()
    tmp_path = short_private_root
    database = tmp_path / "cloud.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    runtime_dsn = private_file(tmp_path / "runtime-dsn", database_url)
    migration_dsn = private_file(tmp_path / "migration-dsn", database_url)
    observer_keyring = keyring_file(
        tmp_path / "observer-keyring",
        UUID("10000000-0000-4000-8000-000000000001"),
    )
    monkeypatch.setenv("HERMES_OBSERVER_KEYRING_FILE", str(observer_keyring))
    await asyncio.to_thread(_migrate_sqlite, migration_dsn)
    password = "observer-e2e-password"
    await asyncio.to_thread(
        _seed_test_server_base,
        runtime_dsn,
        private_file(tmp_path / "initial-password", password),
    )
    tenant_id, device_id = await asyncio.to_thread(
        _seed_legacy_device_authority,
        database_url,
    )
    signing_secret = private_file(
        tmp_path / "connector-signing-secret",
        "c" * 48,
    )
    token_file = tmp_path / "connector-token"
    await asyncio.to_thread(
        _mint_connector_token,
        signing_secret,
        token_file,
        tenant_id=tenant_id,
        device_id=device_id,
    )
    keyring_file(observer_keyring, tenant_id)
    business_secret = private_file(
        tmp_path / "business-signing-secret",
        "b" * 48,
    )

    cloud_engine = build_sqlite_engine(database_url)
    with Session(cloud_engine) as session:
        tenant = session.scalars(
            select(TenantModel).where(TenantModel.tenant_id == tenant_id)
        ).one()
        user = session.scalars(
            select(UserModel).where(UserModel.tenant_id == tenant_id)
        ).one()
        workspace = session.scalars(
            select(WorkspaceModel).where(WorkspaceModel.tenant_id == tenant_id)
        ).one()
        agent = session.scalars(
            select(AgentModel).where(AgentModel.tenant_id == tenant_id)
        ).one()
        seeded = session.scalars(
            select(SessionProjectionModel).where(
                SessionProjectionModel.tenant_id == tenant_id,
                SessionProjectionModel.session_key == SESSION_A,
            )
        ).one()
        assert seeded.agent_id == agent.agent_id
        assert tenant.slug == "interop"
        assert user.subject == "interop@example.test"
        assert workspace.workspace_key == "interop"
        agent_id = agent.agent_id
        for identity in (tenant_id, device_id, agent_id):
            assert_canonical_uuid(str(identity))
        session.add(
            SessionProjectionModel(
                tenant_id=tenant_id,
                session_id=UUID(RUNTIME_SESSION_B),
                session_key=SESSION_B,
                workspace_id=workspace.workspace_id,
                agent_id=agent_id,
                profile=PROFILE,
                title="Second Observer Session",
                state="active",
                revision=1,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                closed_at=None,
                retention_until=datetime.now(UTC) + timedelta(days=30),
            )
        )
        session.commit()

    paths = local_paths(tmp_path)
    host = GatewayExtensionV1TestHost(paths)
    baseline_tasks = frozenset(live_noncurrent_tasks())
    baseline_threads = live_thread_names()
    baseline_fds = open_fd_count()
    gateway_server: RunningAsgiServer | None = None
    business_server: RunningAsgiServer | None = None
    storage: SQLiteStorageComponent | None = None
    storage_task: asyncio.Task[None] | None = None
    local_gateway: LocalGatewayClient | None = None
    local_task: asyncio.Task[None] | None = None
    cloud_client = None
    cloud_task: asyncio.Task[None] | None = None
    try:
        host.start()
        assert host.audits == ["runtime.lifecycle"]

        environment = {
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(signing_secret),
            "HERMES_OBSERVER_KEYRING_FILE": str(observer_keyring),
            "HERMES_RUNTIME_DSN_FILE": str(runtime_dsn),
        }
        intent_snapshot_transmitted = asyncio.Event()
        gateway = build_production_connector_gateway_application(
            environment=environment,
            settings=ConnectorGatewaySettings(heartbeat_interval_ms=20_000),
        )
        crash_authority = None
        if commit_crash is not None:
            real_authority = gateway._gateway_service._transport_cursor_authority
            assert real_authority is not None
            crash_authority = _CommitCrashOnceAuthority(
                real_authority,
                commit_crash,
                failure_gate=(
                    intent_snapshot_transmitted
                    if commit_crash == "subscription_intent"
                    else None
                ),
            )
            gateway._gateway_service._transport_cursor_authority = crash_authority
            gateway._gateway_service._resume_resolver = crash_authority
        business = build_production_business_api_application(
            environment={
                **environment,
                "HERMES_SIGNING_SECRET_FILE": str(business_secret),
            }
        )
        gateway_server = RunningAsgiServer(gateway)
        business_server = RunningAsgiServer(business)
        gateway_port = await gateway_server.start()
        business_port = await business_server.start()

        config = ConnectorConfig(local_discovery_poll_interval_seconds=30.0)
        storage = SQLiteStorageComponent(tmp_path / "connector.sqlite3", config)
        await storage.start()
        storage_task = asyncio.create_task(storage.run(), name="e2e-connector-storage")
        assert await asyncio.wait_for(storage.ready(), timeout=3)

        connector_instance_id = UUID("91000000-0000-4000-8000-000000000001")
        client_instance_id = UUID("92000000-0000-4000-8000-000000000001")
        assert_canonical_uuid(str(connector_instance_id))
        assert_canonical_uuid(str(client_instance_id))
        local_gateway = LocalGatewayClient(
            profile=PROFILE,
            client_instance_id=client_instance_id,
            required_capabilities=("session.observe",),
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
            name="e2e-connector-local-gateway",
        )
        assert await asyncio.wait_for(local_gateway.ready(), timeout=3)
        authority = await local_gateway.current_runtime_authority()
        assert authority is not None
        assert authority.runtime_generation == RUNTIME_GENERATION

        codec = ConnectorProtocolCodec()
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
        transport_loss_observed = asyncio.Event()

        async def reconnect_without_wall_clock_delay(_delay: float) -> None:
            transport_loss_observed.set()

        cloud_client = build_cloud_wss_client(
            config=CloudClientConfig(
                endpoint=f"ws://127.0.0.1:{gateway_port}/api/ws",
                tenant_id=str(tenant_id),
                device_id=str(device_id),
                connector_instance_id=connector_instance_id,
                connector_version="1.0.0",
                negotiation_timeout_seconds=3,
                io_timeout_seconds=3,
            ),
            token_provider=MacOSFileCloudTokenProvider(token_file),
            storage=storage,
            runtime_authority=local_gateway,
            observer_outbound_lane=outbound,
            transport=WebSocketsCloudTransport(),
            codec=codec,
            sleep=reconnect_without_wall_clock_delay,
        )
        snapshot_publish_records: list[ObserverOutboxRecord] = []
        event_publish_records: list[ObserverOutboxRecord] = []
        production_publish_observer_snapshot = cloud_client.publish_observer_snapshot
        production_publish_observer_event = cloud_client.publish_observer_event

        async def observe_snapshot_publish(
            snapshot: SessionSnapshot,
            *,
            force_new_attempt: bool = False,
        ) -> ObserverOutboxRecord:
            record = await production_publish_observer_snapshot(
                snapshot,
                force_new_attempt=force_new_attempt,
            )
            snapshot_publish_records.append(record)
            intent_snapshot_transmitted.set()
            return record

        cloud_client.publish_observer_snapshot = (  # type: ignore[method-assign]
            observe_snapshot_publish
        )

        async def observe_event_publish(event: SessionEvent) -> ObserverOutboxRecord:
            record = await production_publish_observer_event(event)
            event_publish_records.append(record)
            return record

        cloud_client.publish_observer_event = (  # type: ignore[method-assign]
            observe_event_publish
        )
        intents = ObserverIntentLane(
            local_client=observer_client,
            publisher=cloud_client,
        )
        recovery_acknowledged = asyncio.Event()
        reconnect_phase = asyncio.Event()
        reconnected = asyncio.Event()
        reconnect_failed = asyncio.Event()
        reconnect_failures: list[str] = []
        reconciled_intent = asyncio.Event()
        processed_intent = asyncio.Event()
        intent_open_calls = 0
        production_acknowledge = intents.acknowledge
        production_open = intents.open
        production_start = cloud_client.start

        async def observe_production_ack(ack: StreamAck) -> None:
            await production_acknowledge(ack)
            if (
                ack.observer_message_type == "session.snapshot"
                and ack.session_key == SESSION_A
                and ack.event_sequence == 3
            ):
                recovery_acknowledged.set()

        intents.acknowledge = observe_production_ack  # type: ignore[method-assign]

        async def observe_production_open(intent: object) -> None:
            nonlocal intent_open_calls
            await production_open(intent)
            intent_open_calls += 1
            processed_intent.set()
            if reconnected.is_set():
                reconciled_intent.set()

        intents.open = observe_production_open  # type: ignore[method-assign]

        async def observe_production_start() -> None:
            try:
                await production_start()
            except Exception as error:
                if reconnect_phase.is_set():
                    reconnect_failures.append(f"{type(error).__name__}: {error}")
                    reconnect_failed.set()
                raise
            if reconnect_phase.is_set():
                reconnected.set()

        cloud_client.start = observe_production_start  # type: ignore[method-assign]
        cloud_client.bind_observer_intent_lane(intents)
        await cloud_client.start()
        cloud_task = asyncio.create_task(
            cloud_client.run(),
            name="e2e-connector-cloud",
        )

        login_status, _login_body, set_cookies = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/auth/password-login",
            {
                "provider": "basic",
                "username": "interop@example.test",
                "password": password,
                "next": "",
            },
        )
        assert login_status == 200
        cookies = SimpleCookie()
        for value in set_cookies:
            cookies.load(value)
        access_token = cookies["hermes_session_at"].value
        ticket_status, ticket_body, _ticket_cookies = await asyncio.to_thread(
            _post_json,
            f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
            {},
            authorization=f"Bearer {access_token}",
        )
        assert ticket_status == 200
        ticket = ticket_body["ticket"]
        assert isinstance(ticket, str)

        async with connect(
            f"ws://127.0.0.1:{business_port}/api/ws?ticket={ticket}",
            subprotocols=("hermes.tui.v1",),
            open_timeout=3,
            close_timeout=3,
            ping_interval=None,
        ) as websocket:
            ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
            assert ready["params"]["type"] == "gateway.ready"
            connection_before_commit_crash = cloud_client.connection_id
            assert connection_before_commit_crash is not None
            if crash_authority is not None:
                reconnect_phase.set()
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "session.observe.subscribe",
                        "params": {
                            "session_key": SESSION_A,
                            "profile": PROFILE,
                        },
                    },
                    separators=(",", ":"),
                )
            )
            if crash_authority is not None and commit_crash == "subscription_intent":
                await asyncio.wait_for(crash_authority.failed.wait(), timeout=10)
                await asyncio.wait_for(
                    crash_authority.reconnect_resolved.wait(),
                    timeout=10,
                )
                await asyncio.wait_for(reconnected.wait(), timeout=10)
                await asyncio.wait_for(
                    crash_authority.target_committed.wait(), timeout=10
                )
                await asyncio.wait_for(reconciled_intent.wait(), timeout=10)
                assert cloud_client.connection_id != connection_before_commit_crash
                assert crash_authority.reconnect_decision in {
                    "resumed",
                    "reset_required",
                }
                async with asyncio.timeout(10):
                    while True:
                        with Session(cloud_engine) as session:
                            inbox_count = session.scalar(
                                select(func.count()).select_from(ObserverInboxModel)
                            )
                            projection_count = session.scalar(
                                select(func.count()).select_from(ObserverSessionModel)
                            )
                        if inbox_count == 1 and projection_count == 1:
                            break
                        await asyncio.sleep(0.05)
                with Session(cloud_engine) as session:
                    intent_rows = session.execute(
                        select(
                            ObserverSubscriptionIntentModel,
                            ObserverSubscriptionTargetModel,
                        )
                        .join(
                            ObserverSubscriptionTargetModel,
                            (
                                ObserverSubscriptionTargetModel.tenant_id
                                == ObserverSubscriptionIntentModel.tenant_id
                            )
                            & (
                                ObserverSubscriptionTargetModel.target_subscription_id
                                == ObserverSubscriptionIntentModel.target_subscription_id
                            ),
                        )
                        .where(
                            ObserverSubscriptionTargetModel.tenant_id == tenant_id,
                            ObserverSubscriptionTargetModel.profile == PROFILE,
                            ObserverSubscriptionTargetModel.session_key == SESSION_A,
                        )
                        .order_by(
                            ObserverSubscriptionIntentModel.intent_sequence,
                            ObserverSubscriptionIntentModel.request_id,
                        )
                    ).all()
                    intents = tuple(intent for intent, _target in intent_rows)
                    targets = tuple(target for _intent, target in intent_rows)
                    assert 1 <= len(intents) <= 2
                    assert {target.target_subscription_id for target in targets} == {
                        targets[0].target_subscription_id
                    }
                    assert all(
                        target.profile == PROFILE
                        and target.session_key == SESSION_A
                        and target.state == "active"
                        and target.active_ref_count == 1
                        and target.revision == len(intents) + 1
                        for target in targets
                    )
                    assert tuple(intent.intent_sequence for intent in intents) == tuple(
                        range(len(intents))
                    )
                    assert all(
                        intent.message_type == "session.observe.open"
                        for intent in intents
                    )
                    assert sum(intent.dispatch_attempts for intent in intents) == 2
                    effective = tuple(
                        intent for intent in intents if intent.state != "cancelled"
                    )
                    assert len(effective) == 1
                    current = effective[0]
                    assert current.state == "dispatching"
                    assert current.dispatch_connection_id == str(
                        cloud_client.connection_id
                    )
                    assert current.dispatch_sequence is not None
                    if len(intents) == 1:
                        assert current.dispatch_attempts == 2
                        assert current.supersedes_request_id is None
                    else:
                        historical, current = intents
                        assert historical.state == "cancelled"
                        assert historical.dispatch_attempts == 1
                        assert current.dispatch_attempts == 1
                        assert current.supersedes_request_id == historical.request_id
                        assert historical.dispatch_sequence is not None
                        assert (
                            current.dispatch_sequence
                            == historical.dispatch_sequence + 1
                        )
                assert len(snapshot_publish_records) == 2
                first_attempt, duplicate_ensure = snapshot_publish_records
                assert duplicate_ensure.message_id == first_attempt.message_id
                assert (
                    duplicate_ensure.connector_sequence
                    == first_attempt.connector_sequence
                )
                assert first_attempt.state == "pending"
                async with asyncio.timeout(10):
                    while True:
                        durable_record = await storage.get_observer_outbox(
                            first_attempt.message_id
                        )
                        if (
                            durable_record is not None
                            and durable_record.state == "acked"
                        ):
                            break
                        await asyncio.sleep(0.05)
                assert durable_record is not None
                assert durable_record.state == "acked"
                assert host.sessions[SESSION_A].prepare_count == 1
                return
            snapshot = json.loads(
                await asyncio.wait_for(
                    websocket.recv(),
                    timeout=(12 if commit_crash == "subscription_intent" else 10),
                )
            )
            expected_subscribe_response_id = 1
            if commit_crash == "subscription_intent" and "error" in snapshot:
                assert snapshot == {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 4001, "message": "session not found"},
                }
                expected_subscribe_response_id = 2
                await websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": expected_subscribe_response_id,
                            "method": "session.observe.subscribe",
                            "params": {
                                "session_key": SESSION_A,
                                "profile": PROFILE,
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                snapshot = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=10)
                )

            assert snapshot["id"] == expected_subscribe_response_id
            assert "result" in snapshot, snapshot
            result = snapshot["result"]
            assert_canonical_uuid(result["subscription_id"])
            assert result["session_key"] == SESSION_A
            assert result["runtime_session_id"] == RUNTIME_SESSION_A
            assert result["event_sequence"] == 0
            assert result["messages"] == [
                {"role": "assistant", "content": f"权威快照：{SESSION_A}"}
            ]
            if crash_authority is not None:
                with Session(cloud_engine) as session:
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverInboxModel)
                        )
                        == 1
                    )
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverSessionModel)
                        )
                        == 1
                    )
                    if commit_crash == "subscription_intent":
                        intent = session.scalars(
                            select(ObserverSubscriptionIntentModel)
                        ).one()
                        assert intent.dispatch_attempts == 2
                assert host.sessions[SESSION_A].prepare_count == 1
            if crash_authority is not None and commit_crash == "observer_ack":
                await asyncio.wait_for(
                    crash_authority.target_committed.wait(),
                    timeout=10,
                )
                crash_authority.arm()
                first_sequence = host.sessions[SESSION_A].emit(
                    event_type="message.delta",
                    payload={"text": "实时增量：你好，赫尔墨斯"},
                )
                first_event = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=10)
                )
                await asyncio.wait_for(crash_authority.failed.wait(), timeout=10)
                await asyncio.wait_for(
                    crash_authority.reconnect_resolved.wait(),
                    timeout=10,
                )
                await asyncio.wait_for(reconnected.wait(), timeout=10)
                await asyncio.wait_for(reconciled_intent.wait(), timeout=10)
                assert cloud_client.connection_id != connection_before_commit_crash
                assert crash_authority.reconnect_decision == "resumed"
                with Session(cloud_engine) as session:
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverInboxModel)
                        )
                        == 2
                    )
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverSessionModel)
                        )
                        == 1
                    )
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverEventModel)
                        )
                        == 1
                    )
                assert host.sessions[SESSION_A].prepare_count == 1
                second_sequence = host.sessions[SESSION_A].emit(
                    event_type="status.update",
                    payload={"status": "working", "running": True},
                )
                async with asyncio.timeout(10):
                    while len(event_publish_records) < 2 and intents.failure is None:
                        await asyncio.sleep(0.05)
                if intents.failure is not None:
                    checkpoint = await storage.get_cloud_session()
                    observer_outbox = await storage.pending_observer_outbox(
                        limit=16,
                        include_settled=True,
                    )
                    observer_states = tuple(
                        (
                            record.message_id,
                            record.connector_sequence,
                            record.message_type,
                            record.event_sequence,
                            record.state,
                            record.transport_epoch_id,
                        )
                        for record in observer_outbox
                    )
                    transport_frames = tuple(
                        [
                            await storage.transport_frame(record.message_id)
                            for record in observer_outbox
                        ]
                    )
                    pytest.fail(
                        "Observer pump failed after reset: "
                        f"{intents.failure!r}; checkpoint={checkpoint!r}; "
                        f"observer_states={observer_states!r}; "
                        f"transport_states={tuple((frame.message_id, frame.sequence, frame.state) for frame in transport_frames if frame is not None)!r}"
                    )
                assert len(event_publish_records) == 2
                deadline = asyncio.get_running_loop().time() + 10
                while True:
                    with Session(cloud_engine) as session:
                        projected_events = session.scalar(
                            select(func.count()).select_from(ObserverEventModel)
                        )
                    if (
                        projected_events == 2
                        or intents.failure is not None
                        or asyncio.get_running_loop().time() >= deadline
                    ):
                        break
                    await asyncio.sleep(0.05)
                if projected_events != 2:
                    checkpoint = await storage.get_cloud_session()
                    observer_outbox = await storage.pending_observer_outbox(
                        limit=16,
                        include_settled=True,
                    )
                    observer_states = tuple(
                        (
                            record.message_id,
                            record.connector_sequence,
                            record.message_type,
                            record.event_sequence,
                            record.state,
                            record.transport_epoch_id,
                        )
                        for record in observer_outbox
                    )
                    transport_frames = tuple(
                        [
                            await storage.transport_frame(record.message_id)
                            for record in observer_outbox
                        ]
                    )
                    with Session(cloud_engine) as session:
                        cursor = session.scalars(
                            select(ConnectorTransportCursorModel)
                        ).one()
                        receipt_states = tuple(
                            (
                                row.observer_message_id,
                                row.state,
                                row.dispatch_connection_id,
                                row.dispatch_sequence,
                                row.dispatch_attempts,
                            )
                            for row in session.scalars(
                                select(ConnectorObserverReceiptModel).order_by(
                                    ConnectorObserverReceiptModel.created_at
                                )
                            )
                        )
                    pytest.fail(
                        "Second Observer event was not projected after reset: "
                        f"failure={intents.failure!r}; checkpoint={checkpoint!r}; "
                        f"observer_states={observer_states!r}; "
                        f"transport_states={tuple((frame.message_id, frame.sequence, frame.state) for frame in transport_frames if frame is not None)!r}; "
                        f"cloud_cursor={(cursor.next_connector_sequence, cursor.next_cloud_sequence, cursor.state)!r}; "
                        f"receipt_states={receipt_states!r}"
                    )
                second_event = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=10)
                )
            else:
                first_sequence = host.sessions[SESSION_A].emit(
                    event_type="message.delta",
                    payload={"text": "实时增量：你好，赫尔墨斯"},
                )
                second_sequence = host.sessions[SESSION_A].emit(
                    event_type="status.update",
                    payload={"status": "working", "running": True},
                )
                first_event = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=10)
                )
                second_event = json.loads(
                    await asyncio.wait_for(websocket.recv(), timeout=10)
                )
            assert (first_sequence, second_sequence) == (1, 2)
            assert first_event == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "session_id": RUNTIME_SESSION_A,
                    "session_key": SESSION_A,
                    "event_sequence": 1,
                    "payload": {"text": "实时增量：你好，赫尔墨斯"},
                },
            }
            assert second_event == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "status.update",
                    "session_id": RUNTIME_SESSION_A,
                    "session_key": SESSION_A,
                    "event_sequence": 2,
                    "payload": {"status": "working", "running": True},
                },
            }
            if commit_crash is not None:
                with Session(cloud_engine) as session:
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverInboxModel)
                        )
                        == 3
                    )
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverSessionModel)
                        )
                        == 1
                    )
                    assert (
                        session.scalar(
                            select(func.count()).select_from(ObserverEventModel)
                        )
                        == 2
                    )
                assert host.sessions[SESSION_A].prepare_count == 1
                return
            with Session(cloud_engine) as session:
                projected = session.scalars(
                    select(ObserverSessionModel).where(
                        ObserverSessionModel.tenant_id == tenant_id,
                        ObserverSessionModel.agent_id == agent_id,
                        ObserverSessionModel.profile == PROFILE,
                        ObserverSessionModel.session_key == SESSION_A,
                    )
                ).one()
                projected.event_sequence = 1
                session.execute(
                    delete(ObserverEventModel).where(
                        ObserverEventModel.tenant_id == tenant_id,
                        ObserverEventModel.session_id == projected.session_id,
                        ObserverEventModel.event_sequence == 2,
                    )
                )
                session.commit()

            rejected_sequence = host.sessions[SESSION_A].emit(
                event_type="message.delta",
                payload={"text": "制造真实 Cloud 序列缺口"},
            )
            assert rejected_sequence == 3
            assert await asyncio.to_thread(
                host.sessions[SESSION_A].wait_for_prepare_count,
                2,
                10,
            )
            assert await asyncio.to_thread(
                host.sessions[SESSION_A].wait_for_active_count,
                1,
                10,
            )
            await asyncio.wait_for(recovery_acknowledged.wait(), timeout=10)
            rejected = await storage.get_observer_outbox(
                event_publish_records[-1].message_id
            )
            replacement = await storage.get_observer_outbox(
                snapshot_publish_records[-1].message_id
            )
            assert rejected is not None
            assert rejected.state == "rejected"
            assert_canonical_uuid(rejected.message_id)
            assert replacement is not None
            assert replacement.state == "acked"
            assert_canonical_uuid(replacement.message_id)
            assert replacement.connector_sequence == rejected.connector_sequence + 1
            _, second_ticket_body, _ = await asyncio.to_thread(
                _post_json,
                f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
                {},
                authorization=f"Bearer {access_token}",
            )
            async with connect(
                "ws://127.0.0.1:"
                f"{business_port}/api/ws?ticket={second_ticket_body['ticket']}",
                subprotocols=("hermes.tui.v1",),
                open_timeout=3,
                close_timeout=3,
                ping_interval=None,
            ) as same_target_websocket:
                await asyncio.wait_for(same_target_websocket.recv(), timeout=3)
                await same_target_websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "session.observe.subscribe",
                            "params": {
                                "session_key": SESSION_A,
                                "profile": PROFILE,
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                same_target_snapshot = json.loads(
                    await asyncio.wait_for(
                        same_target_websocket.recv(),
                        timeout=10,
                    )
                )
                assert same_target_snapshot["result"]["event_sequence"] == 3
                assert host.sessions[SESSION_A].prepare_count == 2

                _, third_ticket_body, _ = await asyncio.to_thread(
                    _post_json,
                    f"http://127.0.0.1:{business_port}/api/auth/ws-ticket",
                    {},
                    authorization=f"Bearer {access_token}",
                )
                async with connect(
                    "ws://127.0.0.1:"
                    f"{business_port}/api/ws?ticket={third_ticket_body['ticket']}",
                    subprotocols=("hermes.tui.v1",),
                    open_timeout=3,
                    close_timeout=3,
                    ping_interval=None,
                ) as different_target_websocket:
                    await asyncio.wait_for(
                        different_target_websocket.recv(),
                        timeout=3,
                    )
                    await different_target_websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 4,
                                "method": "session.observe.subscribe",
                                "params": {
                                    "session_key": SESSION_B,
                                    "profile": PROFILE,
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                    different_snapshot = json.loads(
                        await asyncio.wait_for(
                            different_target_websocket.recv(),
                            timeout=10,
                        )
                    )
                    assert different_snapshot["result"]["session_key"] == SESSION_B
                    assert different_snapshot["result"]["runtime_session_id"] == (
                        RUNTIME_SESSION_B
                    )
                    assert await asyncio.to_thread(
                        host.sessions[SESSION_B].wait_for_active_count,
                        1,
                        3,
                    )

                    if exercise_reconnect:
                        with Session(cloud_engine) as session:
                            inbox_before_reconnect = session.scalar(
                                select(func.count()).select_from(ObserverInboxModel)
                            )
                        connection_before_reconnect = cloud_client.connection_id
                        assert connection_before_reconnect is not None
                        reconnect_phase.set()
                        active_connection = cloud_client._connection
                        assert active_connection is not None
                        await active_connection.close(
                            code=1011,
                            reason="e2e_transport_loss",
                            timeout_seconds=3,
                        )
                        await asyncio.wait_for(
                            transport_loss_observed.wait(),
                            timeout=10,
                        )
                        done, pending = await asyncio.wait(
                            {
                                asyncio.create_task(reconnected.wait()),
                                asyncio.create_task(reconnect_failed.wait()),
                            },
                            timeout=10,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for pending_task in pending:
                            pending_task.cancel()
                        assert done, "Connector did not finish a reconnect attempt"
                        assert reconnected.is_set(), reconnect_failures[0]
                        assert cloud_client.connection_id is not None
                        assert cloud_client.connection_id != connection_before_reconnect
                        assert host.sessions[SESSION_A].prepare_count == 2
                        assert host.sessions[SESSION_B].prepare_count == 1
                        with Session(cloud_engine) as session:
                            assert (
                                session.scalar(
                                    select(func.count()).select_from(ObserverInboxModel)
                                )
                                == inbox_before_reconnect
                            )

                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 5,
                                "method": "session.observe.unsubscribe",
                                "params": {
                                    "subscription_id": result["subscription_id"]
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                    assert (
                        json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))[
                            "result"
                        ]
                        == {}
                    )
                    assert host.sessions[SESSION_A].active_count == 1

                    await same_target_websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 6,
                                "method": "session.observe.unsubscribe",
                                "params": {
                                    "subscription_id": same_target_snapshot["result"][
                                        "subscription_id"
                                    ]
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                    assert (
                        json.loads(
                            await asyncio.wait_for(
                                same_target_websocket.recv(),
                                timeout=3,
                            )
                        )["result"]
                        == {}
                    )
                    assert await asyncio.to_thread(
                        host.sessions[SESSION_A].wait_for_active_count,
                        0,
                        3,
                    )

                    await different_target_websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 7,
                                "method": "session.observe.unsubscribe",
                                "params": {
                                    "subscription_id": different_snapshot["result"][
                                        "subscription_id"
                                    ]
                                },
                            },
                            separators=(",", ":"),
                        )
                    )
                    assert (
                        json.loads(
                            await asyncio.wait_for(
                                different_target_websocket.recv(),
                                timeout=3,
                            )
                        )["result"]
                        == {}
                    )
                    assert await asyncio.to_thread(
                        host.sessions[SESSION_B].wait_for_active_count,
                        0,
                        3,
                    )

        record = await storage.get_observer_outbox(
            snapshot_publish_records[0].message_id
        )
        assert record is not None
        assert record.state == "acked"
        assert_canonical_uuid(record.message_id)

        with Session(cloud_engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(ObserverInboxModel))
                == 5
            )
            projected = session.scalars(
                select(ObserverSessionModel).where(
                    ObserverSessionModel.tenant_id == tenant_id,
                    ObserverSessionModel.agent_id == agent_id,
                    ObserverSessionModel.profile == PROFILE,
                    ObserverSessionModel.session_key == SESSION_A,
                )
            ).one()
            assert projected.runtime_generation == RUNTIME_GENERATION
            assert projected.runtime_session_id == RUNTIME_SESSION_A
            assert projected.event_sequence == 3
            target = session.scalars(
                select(ObserverSubscriptionTargetModel).where(
                    ObserverSubscriptionTargetModel.session_key == SESSION_A
                )
            ).one()
            intent_states = tuple(
                (
                    intent.intent_sequence,
                    intent.message_type,
                    intent.state,
                    intent.dispatch_attempts,
                )
                for intent in session.scalars(
                    select(ObserverSubscriptionIntentModel)
                    .where(
                        ObserverSubscriptionIntentModel.target_subscription_id
                        == target.target_subscription_id
                    )
                    .order_by(ObserverSubscriptionIntentModel.intent_sequence)
                )
            )
        assert host.sessions[SESSION_A].prepare_count == 2, intent_states
        assert host.sessions[SESSION_B].prepare_count == 1
    finally:
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
        if not exercise_reconnect:
            added_tasks = frozenset(live_noncurrent_tasks()) - baseline_tasks
            assert not added_tasks, tuple(task.get_name() for task in added_tasks)
            added_threads = live_thread_names() - baseline_threads
            assert all(
                name == "AnyIO worker thread" or name.startswith("asyncio_")
                for name in added_threads
            ), added_threads
            assert open_fd_count() <= baseline_fds
            for local_directory in (
                paths.local_gateway_registry_directory,
                paths.local_gateway_socket_directory,
                paths.control_registry_directory,
                paths.control_socket_directory,
                paths.observer_registry_directory,
                paths.observer_socket_directory,
            ):
                assert not local_directory.exists() or not any(
                    local_directory.iterdir()
                )
