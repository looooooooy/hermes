from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from hermes_cloud.application.connector_gateway import ConnectorGatewaySettings
from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.entrypoints.connector_gateway.bootstrap import (
    build_production_connector_gateway_application,
)
from hermes_cloud.modules.device.domain import (
    DeviceCredential,
    DeviceCredentialStatus,
    PairingOffer,
    fingerprint_ed25519_public_key,
)
from hermes_cloud.modules.device.ports import (
    ActivatePairingCommand,
    ClaimPairingCommand,
    ConfirmPairingCommand,
    PairingMutation,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    ConnectorTransportCursorModel,
    ControlCommandModel,
    DeviceModel,
    TenantModel,
    UserModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteOperationScopedPairingRepository,
)
from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.websocket_transport import (
    WebSocketsCloudTransport,
)
from hermes_connector.domain.cloud_protocol import (
    ConnectorHeartbeat,
    ConnectorHello,
    ResumePosition,
)
from hermes_connector.domain.contract_messages import CloudEnvelope
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 7, 31, 0, 0, 0, tzinfo=UTC)
CONNECTOR_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
ROOT = Path(__file__).parents[3]
MIGRATE_SQLITE = (
    ROOT / "hermes-cloud" / "deploy" / "test_server" / "scripts" / "migrate_sqlite.py"
)
TOKEN_MINT = (
    ROOT
    / "hermes-cloud"
    / "deploy"
    / "test_server"
    / "scripts"
    / "mint_connector_token.py"
)
SEED_TEST_DATA = (
    ROOT / "hermes-cloud" / "deploy" / "test_server" / "scripts" / "seed_test_data.py"
)
PUBLIC_KEY = bytes(range(32))


class _HeartbeatGate:
    def __init__(self) -> None:
        self._permits = asyncio.Semaphore(0)

    async def __call__(self, _delay: float) -> None:
        await self._permits.acquire()

    def release(self) -> None:
        self._permits.release()


def _write_private(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    assert path.stat().st_mode & 0o777 == 0o600
    return path


def _migrate_sqlite(
    migration_dsn: Path,
    observer_keyring: Path | None = None,
) -> tuple[str, str]:
    environment = {
        **os.environ,
        "HERMES_MIGRATION_DSN_FILE": str(migration_dsn),
    }
    if observer_keyring is not None:
        environment["HERMES_OBSERVER_KEYRING_FILE"] = str(observer_keyring)
    result = subprocess.run(
        (
            sys.executable,
            str(MIGRATE_SQLITE),
            "--apply",
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout, result.stderr


def _generate_test_certificate(directory: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required for the real WSS interoperability test")
    certificate = directory / "localhost.crt"
    private_key = directory / "localhost.key"
    subprocess.run(
        (
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ),
        check=True,
        capture_output=True,
    )
    return certificate, private_key


def _mint_connector_token(
    signing_secret: Path,
    token_file: Path,
    *,
    tenant_id: UUID,
    device_id: UUID,
) -> tuple[str, str, str]:
    result = subprocess.run(
        (
            sys.executable,
            str(TOKEN_MINT),
            "--apply",
            "--output",
            str(token_file),
        ),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(signing_secret),
            "HERMES_CONNECTOR_TOKEN_TENANT_ID": str(tenant_id),
            "HERMES_CONNECTOR_TOKEN_DEVICE_ID": str(device_id),
            "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
        },
    )
    return token_file.read_text().strip(), result.stdout, result.stderr


def _seed_test_server_base(
    bootstrap_dsn: Path,
    initial_password: Path,
) -> tuple[str, str]:
    result = subprocess.run(
        (
            sys.executable,
            str(SEED_TEST_DATA),
            "--apply",
        ),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HERMES_BOOTSTRAP_DSN_FILE": str(bootstrap_dsn),
            "HERMES_INITIAL_USER_PASSWORD_FILE": str(initial_password),
            "HERMES_SEED_TENANT_SLUG": "interop",
            "HERMES_SEED_TENANT_DISPLAY_NAME": "Interop",
            "HERMES_SEED_USERNAME": "interop@example.test",
            "HERMES_SEED_USER_DISPLAY_NAME": "Interop Owner",
            "HERMES_SEED_WORKSPACE_KEY": "interop",
            "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Interop",
            "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
            "HERMES_SEED_AGENT_KEY": "agent-interop",
            "HERMES_SEED_DEVICE_KEY": "device-interop",
        },
    )
    return result.stdout, result.stderr


def _pairing_mutation(
    *,
    operation: str,
    digit: str,
    expected_revision: int,
    now: datetime,
) -> PairingMutation:
    return PairingMutation(
        pairing_mutation_id=UUID(
            f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-{digit * 12}"
        ),
        operation=operation,
        idempotency_key_digest=digit * 64,
        principal_digest="e" * 64,
        request_digest=digit * 64,
        expected_revision=expected_revision,
        created_at=now,
        expires_at=now + timedelta(days=1),
    )


def _seed_legacy_device_authority(database_url: str) -> tuple[UUID, UUID]:
    engine = build_sqlite_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        with factory.begin() as session:
            tenant = session.scalars(
                select(TenantModel).where(TenantModel.slug == "interop")
            ).one()
            owner = session.scalars(
                select(UserModel).where(
                    UserModel.tenant_id == tenant.tenant_id,
                    UserModel.subject == "interop@example.test",
                )
            ).one()
            workspace = session.scalars(
                select(WorkspaceModel).where(
                    WorkspaceModel.tenant_id == tenant.tenant_id,
                    WorkspaceModel.workspace_key == "interop",
                )
            ).one()
            agent = session.scalars(
                select(AgentModel).where(
                    AgentModel.tenant_id == tenant.tenant_id,
                    AgentModel.agent_key == "agent-interop",
                )
            ).one()
            device = session.scalars(
                select(DeviceModel).where(
                    DeviceModel.tenant_id == tenant.tenant_id,
                    DeviceModel.device_key == "device-interop",
                )
            ).one()
            session.delete(device)

        seed_now = datetime.now(UTC)
        offer_id = UUID("11111111-1111-4111-8111-111111111111")
        pairing_session_id = UUID("55555555-5555-4555-8555-555555555555")
        challenge_id = UUID("77777777-7777-4777-8777-777777777777")
        credential_id = UUID("88888888-8888-4888-8888-888888888888")
        fingerprint = fingerprint_ed25519_public_key(PUBLIC_KEY)
        repository.create_offer(
            PairingOffer(
                pairing_offer_id=offer_id,
                pairing_code_digest="a" * 64,
                bootstrap_secret_digest="b" * 64,
                algorithm="ed25519",
                public_key=PUBLIC_KEY,
                credential_fingerprint=fingerprint,
                key_id=fingerprint,
                device_key="device-interop",
                device_name="Interop Connector",
                platform="macos",
                connector_version="1.0.0",
                state=PairingSessionState.PENDING,
                revision=0,
                expires_at=seed_now + timedelta(minutes=5),
                claimed_at=None,
                created_at=seed_now,
            ),
            mutation=_pairing_mutation(
                operation="create",
                digit="2",
                expected_revision=0,
                now=seed_now,
            ),
        )
        repository.claim_offer(
            ClaimPairingCommand(
                pairing_session_id=pairing_session_id,
                tenant_id=tenant.tenant_id,
                owner_user_id=owner.user_id,
                workspace_id=workspace.workspace_id,
                agent_id=agent.agent_id,
                device_id=device.device_id,
                device_display_name="Interop Connector",
                scopes=("session.observe",),
                pairing_code_digest="a" * 64,
                expected_revision=0,
                now=seed_now + timedelta(seconds=1),
            ),
            mutation=_pairing_mutation(
                operation="claim",
                digit="3",
                expected_revision=0,
                now=seed_now + timedelta(seconds=1),
            ),
        )
        repository.confirm_owner(
            ConfirmPairingCommand(
                tenant_id=tenant.tenant_id,
                owner_user_id=owner.user_id,
                pairing_session_id=pairing_session_id,
                credential_fingerprint=fingerprint,
                expected_revision=1,
                challenge_id=challenge_id,
                challenge_digest=sha256(b"interop-challenge").hexdigest(),
                challenge_expires_at=seed_now + timedelta(seconds=52),
                now=seed_now + timedelta(seconds=2),
            ),
            mutation=_pairing_mutation(
                operation="confirm",
                digit="4",
                expected_revision=1,
                now=seed_now + timedelta(seconds=2),
            ),
        )
        repository.activate_verified_credential(
            ActivatePairingCommand(
                tenant_id=tenant.tenant_id,
                pairing_offer_id=offer_id,
                pairing_session_id=pairing_session_id,
                bootstrap_secret_digest="b" * 64,
                challenge_id=challenge_id,
                challenge_digest=sha256(b"interop-challenge").hexdigest(),
                confirmation_digest=sha256(b"interop-confirmation").hexdigest(),
                credential=DeviceCredential(
                    tenant_id=tenant.tenant_id,
                    credential_id=credential_id,
                    device_id=device.device_id,
                    algorithm="ed25519",
                    key_id=fingerprint,
                    public_key=PUBLIC_KEY,
                    credential_fingerprint=fingerprint,
                    status=DeviceCredentialStatus.ACTIVE,
                    issued_at=seed_now + timedelta(seconds=3),
                    expires_at=None,
                    revoked_at=None,
                ),
                expected_revision=2,
                now=seed_now + timedelta(seconds=3),
            ),
            mutation=_pairing_mutation(
                operation="proof",
                digit="5",
                expected_revision=2,
                now=seed_now + timedelta(seconds=3),
            ),
        )
        return tenant.tenant_id, device.device_id
    finally:
        engine.dispose()


def _hello_frame(
    codec: ConnectorProtocolCodec,
    *,
    tenant_id: UUID,
    device_id: UUID,
    connector_instance_id: UUID = CONNECTOR_INSTANCE_ID,
    runtime_generation: str = "runtime-interop",
    mode: str = "fresh",
    previous_connection_id: UUID | None = None,
    next_outbound_sequence: int = 0,
    next_inbound_sequence: int = 0,
) -> bytes:
    hello = ConnectorHello(
        connector_instance_id=connector_instance_id,
        connector_version="1.0.0",
        runtime_generation=runtime_generation,
        required_capabilities=("session.observe",),
        optional_capabilities=(),
        resume=ResumePosition(
            mode=mode,
            previous_connection_id=previous_connection_id,
            next_outbound_sequence=next_outbound_sequence,
            next_inbound_sequence=next_inbound_sequence,
        ),
    )
    return codec.encode_envelope(
        CloudEnvelope(
            contract_version=1,
            message_id=UUID("22222222-2222-4222-8222-222222222222"),
            message_type="connector.hello",
            tenant_id=str(tenant_id),
            device_id=str(device_id),
            sequence=next_outbound_sequence,
            sent_at=NOW,
            payload=codec.hello_payload(hello),
        )
    )


@pytest.mark.asyncio
async def test_real_connector_transport_and_codec_interoperate_with_cloud_gateway(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    certificate, private_key = _generate_test_certificate(tmp_path)
    client_tls = ssl.create_default_context(cafile=str(certificate))
    database = tmp_path / "runtime.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    migration_dsn = _write_private(tmp_path / "migration-dsn", database_url)
    runtime_dsn = _write_private(tmp_path / "runtime-dsn", database_url)
    observer_keyring = _write_private(
        tmp_path / "observer-keyring",
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    "10000000-0000-4000-8000-000000000001": {
                        "current": "interop-v1",
                        "keys": {
                            "interop-v1": base64.b64encode(b"k" * 32).decode("ascii")
                        },
                    }
                },
            },
            separators=(",", ":"),
        ),
    )
    migration_stdout, migration_stderr = await asyncio.to_thread(
        _migrate_sqlite,
        migration_dsn,
        observer_keyring,
    )
    assert migration_stdout.startswith("sqlite_migration_mode=apply ")
    assert migration_stderr == ""
    assert database_url not in migration_stdout + migration_stderr
    assert database.stat().st_mode & 0o777 == 0o660

    initial_password = _write_private(
        tmp_path / "initial-password",
        "interop-test-password",
    )
    seed_stdout, seed_stderr = await asyncio.to_thread(
        _seed_test_server_base,
        runtime_dsn,
        initial_password,
    )
    assert seed_stdout.startswith("seed_mode=apply created=")
    assert seed_stdout.endswith(" existing=0\n")
    assert seed_stderr == ""
    assert database_url not in seed_stdout + seed_stderr
    tenant_id, device_id = await asyncio.to_thread(
        _seed_legacy_device_authority,
        database_url,
    )

    signing_secret = _write_private(
        tmp_path / "connector-signing-secret",
        "s" * 32,
    )
    token_file = tmp_path / "connector.token"
    connector_token, mint_stdout, mint_stderr = await asyncio.to_thread(
        _mint_connector_token,
        signing_secret,
        token_file,
        tenant_id=tenant_id,
        device_id=device_id,
    )
    assert mint_stdout == "mint_mode=apply token_written=true\n"
    assert connector_token not in mint_stdout + mint_stderr
    assert token_file.stat().st_mode & 0o777 == 0o600

    heartbeat_gate = _HeartbeatGate()
    app = build_production_connector_gateway_application(
        environment={
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(signing_secret),
            "HERMES_OBSERVER_KEYRING_FILE": str(observer_keyring),
            "HERMES_RUNTIME_DSN_FILE": str(runtime_dsn),
        },
        settings=ConnectorGatewaySettings(heartbeat_interval_ms=5_000),
        sleep=heartbeat_gate,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_config=None,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(private_key),
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            await asyncio.sleep(0)

        codec = ConnectorProtocolCodec()
        transport = WebSocketsCloudTransport(ssl_context=client_tls)
        connection = await transport.connect(
            f"wss://localhost:{port}/api/ws",
            token=connector_token,
        )
        await connection.send(
            _hello_frame(
                codec,
                tenant_id=tenant_id,
                device_id=device_id,
            ),
            timeout_seconds=2,
        )

        welcome_envelope = codec.decode_envelope(
            await connection.receive(timeout_seconds=2)
        )
        welcome = codec.decode_welcome_payload(welcome_envelope.payload)
        heartbeat_gate.release()
        cloud_heartbeat_envelope = codec.decode_envelope(
            await connection.receive(timeout_seconds=2)
        )
        cloud_heartbeat = codec.decode_heartbeat_payload(
            cloud_heartbeat_envelope.payload
        )

        connector_heartbeat = ConnectorHeartbeat(
            connection_id=welcome.connection_id,
            sender_role="connector",
            observed_at=NOW,
            next_outbound_sequence=1,
            next_inbound_sequence=2,
            session_state="active",
        )
        await connection.send(
            codec.encode_envelope(
                CloudEnvelope(
                    contract_version=1,
                    message_id=UUID("33333333-3333-4333-8333-333333333333"),
                    message_type="connector.heartbeat",
                    tenant_id=str(tenant_id),
                    device_id=str(device_id),
                    sequence=1,
                    sent_at=NOW,
                    payload=codec.heartbeat_payload(connector_heartbeat),
                )
            ),
            timeout_seconds=2,
        )
        with pytest.raises(TimeoutError):
            await connection.receive(timeout_seconds=0.05)

        assert welcome.resume_decision == "fresh"
        assert cloud_heartbeat.sender_role == "cloud"
        assert cloud_heartbeat.session_state == "active"
        assert cloud_heartbeat.next_outbound_sequence == 1
        assert cloud_heartbeat.next_inbound_sequence == 1
        await connection.close(code=1000, reason="", timeout_seconds=2)

        inspection_engine = build_sqlite_engine(database_url)

        async def wait_for_cursor(
            *,
            state: str,
            runtime_generation: str,
            next_connector_sequence: int,
            next_cloud_sequence: int,
        ) -> ConnectorTransportCursorModel:
            async with asyncio.timeout(2):
                while True:
                    with Session(inspection_engine) as session:
                        row = session.get(
                            ConnectorTransportCursorModel,
                            (tenant_id, device_id),
                        )
                        if (
                            row is not None
                            and row.state == state
                            and row.runtime_generation == runtime_generation
                            and row.next_connector_sequence == next_connector_sequence
                            and row.next_cloud_sequence == next_cloud_sequence
                        ):
                            session.expunge(row)
                            return row
                    await asyncio.sleep(0.01)

        await wait_for_cursor(
            state="offline",
            runtime_generation="runtime-interop",
            next_connector_sequence=2,
            next_cloud_sequence=2,
        )

        rollover = await transport.connect(
            f"wss://localhost:{port}/api/ws",
            token=connector_token,
        )
        await rollover.send(
            _hello_frame(
                codec,
                tenant_id=tenant_id,
                device_id=device_id,
                runtime_generation="runtime-interop-rollover",
                mode="resume",
                previous_connection_id=welcome.connection_id,
                next_outbound_sequence=2,
                next_inbound_sequence=2,
            ),
            timeout_seconds=2,
        )
        rollover_welcome_envelope = codec.decode_envelope(
            await rollover.receive(timeout_seconds=2)
        )
        rollover_welcome = codec.decode_welcome_payload(
            rollover_welcome_envelope.payload
        )
        assert rollover_welcome.resume_decision == "fresh"
        assert rollover_welcome.next_connector_sequence == 0
        assert rollover_welcome.next_cloud_sequence == 0

        heartbeat_gate.release()
        rollover_cloud_heartbeat_envelope = codec.decode_envelope(
            await rollover.receive(timeout_seconds=2)
        )
        rollover_cloud_heartbeat = codec.decode_heartbeat_payload(
            rollover_cloud_heartbeat_envelope.payload
        )
        assert rollover_cloud_heartbeat_envelope.sequence == 0
        assert rollover_cloud_heartbeat.next_outbound_sequence == 0
        assert rollover_cloud_heartbeat.next_inbound_sequence == 0

        await rollover.send(
            codec.encode_envelope(
                CloudEnvelope(
                    contract_version=1,
                    message_id=UUID("33333333-3333-4333-8333-333333333334"),
                    message_type="connector.heartbeat",
                    tenant_id=str(tenant_id),
                    device_id=str(device_id),
                    sequence=0,
                    sent_at=NOW,
                    payload=codec.heartbeat_payload(
                        ConnectorHeartbeat(
                            connection_id=rollover_welcome.connection_id,
                            sender_role="connector",
                            observed_at=NOW,
                            next_outbound_sequence=0,
                            next_inbound_sequence=1,
                            session_state="active",
                        )
                    ),
                )
            ),
            timeout_seconds=2,
        )
        await wait_for_cursor(
            state="active",
            runtime_generation="runtime-interop-rollover",
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        await rollover.close(code=1000, reason="", timeout_seconds=2)
        await wait_for_cursor(
            state="offline",
            runtime_generation="runtime-interop-rollover",
            next_connector_sequence=1,
            next_cloud_sequence=1,
        )
        inspection_engine.dispose()
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)

    engine = build_sqlite_engine(database_url)
    try:
        with Session(engine) as session:
            control_command_count = session.scalar(
                select(func.count()).select_from(ControlCommandModel)
            )
            assert control_command_count == 0
    finally:
        engine.dispose()
