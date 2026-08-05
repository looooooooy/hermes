from __future__ import annotations

import base64
import json
import runpy
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.adapters.business_api_runtime import (
    build_production_business_api_application,
)
from hermes_cloud.adapters.connector_gateway_runtime import (
    build_production_connector_gateway_application,
)
from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.entrypoints.connector_gateway.bootstrap import create_app
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
    ControlCommandModel,
    DeviceModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteOperationScopedPairingRepository,
)

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
TENANT = "android-test"
DEVICE = "android-device"
SESSION_KEY = "android-bootstrap"
CLIENT_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"
CONNECTOR_INSTANCE_ID = "22222222-2222-4222-8222-222222222222"
LEASE_ID = "33333333-3333-4333-8333-333333333333"
CONTROL_METHODS = [
    "session.control.acquire",
    "session.control.renew",
    "session.control.release",
    "session.control.status",
    "session.command.status",
    "prompt.submit",
    "session.interrupt",
    "session.steer",
    "approval.respond",
    "clarify.respond",
]
PUBLIC_KEY = bytes(range(32))


def _write_private(path: Path, value: str) -> str:
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


def _observer_keyring(path: Path) -> str:
    return _write_private(
        path,
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    "10000000-0000-4000-8000-000000000001": {
                        "current": "test-v1",
                        "keys": {
                            "test-v1": base64.b64encode(b"k" * 32).decode("ascii")
                        },
                    }
                },
            }
        ),
    )


def _hello(*, tenant_id: UUID, device_id: UUID) -> dict[str, object]:
    return {
        "contract_version": 1,
        "message_id": "44444444-4444-4444-8444-444444444444",
        "message_type": "connector.hello",
        "tenant_id": str(tenant_id),
        "device_id": str(device_id),
        "sequence": 0,
        "sent_at": "2026-07-31T12:00:00Z",
        "payload": {
            "connector_instance_id": CONNECTOR_INSTANCE_ID,
            "connector_version": "1.0.0",
            "runtime_generation": "runtime-owner-control-e2e",
            "required_capabilities": [
                "session.catalog.v1",
                "session.observe",
                "session.control",
            ],
            "optional_capabilities": [],
            "resume": {
                "mode": "fresh",
                "next_outbound_sequence": 0,
                "next_inbound_sequence": 0,
            },
        },
    }


def _catalog_snapshot(*, tenant_id: UUID, device_id: UUID) -> dict[str, object]:
    return {
        "contract_version": 1,
        "message_id": "44444444-4444-4444-8444-444444444445",
        "message_type": "session.catalog.snapshot.page",
        "tenant_id": str(tenant_id),
        "device_id": str(device_id),
        "sequence": 1,
        "sent_at": "2026-07-31T12:00:01Z",
        "payload": {
            "profile": "default",
            "runtime_generation": "runtime-owner-control-e2e",
            "snapshot_id": "55555555-5555-4555-8555-555555555555",
            "catalog_revision": 1,
            "page_index": 0,
            "is_last": True,
            "sessions": [
                {
                    "session_key": SESSION_KEY,
                    "surface": "hermes-cli",
                    "authority_revision": 1,
                    "available_actions": [
                        "prompt.submit",
                        "session.interrupt",
                    ],
                }
            ],
        },
    }


def _result(operation: str) -> dict[str, object]:
    if operation == "control.transport.open":
        return {"attached": True, "connection_role": "control"}
    if operation == "session.control.acquire":
        return {
            "lease_id": LEASE_ID,
            "expires_at_epoch_ms": 4_102_444_800_000,
            "control_revision": 1,
            "controller_kind": "mobile",
            "controller_label": "Android Test",
            "pending_input": None,
        }
    if operation == "session.control.status":
        return {
            "controller_kind": "mobile",
            "controller_label": "Android Test",
            "control_revision": 1,
            "lease_expires_at_epoch_ms": 4_102_444_800_000,
            "pending_input": None,
        }
    if operation == "control.transport.close":
        return {"closed": True}
    raise AssertionError(operation)


def _serve_owner_control(
    connector,
    *,
    tenant_id: UUID,
    device_id: UUID,
    operations: list[str],
    errors: list[BaseException],
) -> None:
    # Hello consumes sequence 0 and the catalog snapshot consumes sequence 1.
    sequence = 2
    try:
        while True:
            envelope = connector.receive_json()
            if envelope["message_type"] != "control.request":
                continue
            request = envelope["payload"]
            operation = request["operation"]
            operations.append(operation)
            connector.send_json(
                {
                    "contract_version": 1,
                    "message_id": str(uuid4()),
                    "message_type": "control.response",
                    "tenant_id": str(tenant_id),
                    "device_id": str(device_id),
                    "sequence": sequence,
                    "sent_at": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "idempotency_key": request["request_id"],
                    "payload": {
                        "request_id": request["request_id"],
                        "control_transport_id": request["control_transport_id"],
                        "operation": operation,
                        "state": "succeeded",
                        "completed_at": datetime.now(UTC)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z"),
                        "result": _result(operation),
                    },
                }
            )
            sequence += 1
            if operation == "control.transport.close":
                return
    except BaseException as error:  # noqa: BLE001 - surfaced in test thread
        errors.append(error)


def _control_ticket(
    client: TestClient,
    *,
    session_id: str,
    agent_id: UUID,
) -> str:
    response = client.post(
        "/api/auth/ws-ticket",
        json={
            "connection_role": "control",
            "client_instance_id": CLIENT_INSTANCE_ID,
            "session_id": session_id,
            "agent_id": str(agent_id),
        },
        headers={"Origin": "https://business"},
    )
    assert response.status_code == 200
    return str(response.json()["ticket"])


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


def _activate_owner_control_device(
    database_url: str,
) -> tuple[UUID, UUID, UUID, UUID]:
    engine = build_sqlite_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        with factory.begin() as session:
            tenant = session.scalars(
                select(TenantModel).where(TenantModel.slug == TENANT)
            ).one()
            owner = session.scalars(
                select(UserModel).where(
                    UserModel.tenant_id == tenant.tenant_id,
                    UserModel.subject == "android-user",
                )
            ).one()
            workspace = session.scalars(
                select(WorkspaceModel).where(
                    WorkspaceModel.tenant_id == tenant.tenant_id,
                    WorkspaceModel.workspace_key == "android",
                )
            ).one()
            agent = session.scalars(
                select(AgentModel).where(
                    AgentModel.tenant_id == tenant.tenant_id,
                    AgentModel.agent_key == "android-agent",
                )
            ).one()
            device = session.scalars(
                select(DeviceModel).where(
                    DeviceModel.tenant_id == tenant.tenant_id,
                    DeviceModel.device_key == DEVICE,
                )
            ).one()
            session.delete(device)

        paired_at = datetime.now(UTC)
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
                device_key=DEVICE,
                device_name="Android Test Connector",
                platform="macos",
                connector_version="1.0.0",
                state=PairingSessionState.PENDING,
                revision=0,
                expires_at=paired_at + timedelta(minutes=5),
                claimed_at=None,
                created_at=paired_at,
            ),
            mutation=_pairing_mutation(
                operation="create",
                digit="2",
                expected_revision=0,
                now=paired_at,
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
                device_display_name="Android Test Connector",
                scopes=("session.observe", "session.control.request"),
                pairing_code_digest="a" * 64,
                expected_revision=0,
                now=paired_at + timedelta(seconds=1),
            ),
            mutation=_pairing_mutation(
                operation="claim",
                digit="3",
                expected_revision=0,
                now=paired_at + timedelta(seconds=1),
            ),
        )
        challenge_digest = sha256(b"owner-control-challenge").hexdigest()
        repository.confirm_owner(
            ConfirmPairingCommand(
                tenant_id=tenant.tenant_id,
                owner_user_id=owner.user_id,
                pairing_session_id=pairing_session_id,
                credential_fingerprint=fingerprint,
                expected_revision=1,
                challenge_id=challenge_id,
                challenge_digest=challenge_digest,
                challenge_expires_at=paired_at + timedelta(seconds=52),
                now=paired_at + timedelta(seconds=2),
            ),
            mutation=_pairing_mutation(
                operation="confirm",
                digit="4",
                expected_revision=1,
                now=paired_at + timedelta(seconds=2),
            ),
        )
        repository.activate_verified_credential(
            ActivatePairingCommand(
                tenant_id=tenant.tenant_id,
                pairing_offer_id=offer_id,
                pairing_session_id=pairing_session_id,
                bootstrap_secret_digest="b" * 64,
                challenge_id=challenge_id,
                challenge_digest=challenge_digest,
                confirmation_digest=sha256(b"owner-control-confirmation").hexdigest(),
                credential=DeviceCredential(
                    tenant_id=tenant.tenant_id,
                    credential_id=credential_id,
                    device_id=device.device_id,
                    algorithm="ed25519",
                    key_id=fingerprint,
                    public_key=PUBLIC_KEY,
                    credential_fingerprint=fingerprint,
                    status=DeviceCredentialStatus.ACTIVE,
                    issued_at=paired_at + timedelta(seconds=3),
                    expires_at=None,
                    revoked_at=None,
                ),
                expected_revision=2,
                now=paired_at + timedelta(seconds=3),
            ),
            mutation=_pairing_mutation(
                operation="proof",
                digit="5",
                expected_revision=2,
                now=paired_at + timedelta(seconds=3),
            ),
        )
        return (
            tenant.tenant_id,
            device.device_id,
            credential_id,
            agent.agent_id,
        )
    finally:
        engine.dispose()


def test_sqlite_owner_control_is_advertised_only_for_paired_exact_connector() -> None:
    migrate_main = runpy.run_path(
        str(SCRIPTS / "migrate_sqlite.py"),
        run_name="hermes_cloud_owner_control_e2e_migration",
    )["main"]
    seed_main = runpy.run_path(
        str(SCRIPTS / "seed_test_data.py"),
        run_name="hermes_cloud_owner_control_e2e_seed",
    )["main"]

    with (
        tempfile.TemporaryDirectory() as temporary,
        tempfile.TemporaryDirectory(prefix="hc-", dir="/tmp") as runtime,
    ):
        directory = Path(temporary)
        runtime_directory = Path(runtime)
        runtime_directory.chmod(0o700)
        database = directory / "hermes-cloud.db"
        database_url = f"sqlite+pysqlite:///{database}"
        migration_dsn = _write_private(
            directory / "migration-dsn",
            database_url,
        )
        bootstrap_dsn = _write_private(
            directory / "bootstrap-dsn",
            database_url,
        )
        runtime_dsn = _write_private(
            directory / "runtime-dsn",
            database_url,
        )
        password = _write_private(
            directory / "initial-password",
            "correct-password",
        )
        business_secret = _write_private(
            directory / "business-signing",
            "business-owner-control-signing-material-32-bytes",
        )
        connector_secret_value = "connector-owner-control-signing-material-32-bytes"
        connector_secret = _write_private(
            directory / "connector-signing",
            connector_secret_value,
        )
        observer_keyring = _observer_keyring(directory / "observer-keyring.json")
        socket_path = runtime_directory / "owner-control.sock"

        migrate_main(
            ["--apply"],
            environment={
                "HERMES_MIGRATION_DSN_FILE": migration_dsn,
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
            },
        )
        seed_main(
            ["--apply"],
            environment={
                "HERMES_BOOTSTRAP_DSN_FILE": bootstrap_dsn,
                "HERMES_INITIAL_USER_PASSWORD_FILE": password,
                "HERMES_SEED_TENANT_SLUG": TENANT,
                "HERMES_SEED_TENANT_DISPLAY_NAME": "Android Test",
                "HERMES_SEED_USERNAME": "android-user",
                "HERMES_SEED_USER_DISPLAY_NAME": "Android User",
                "HERMES_SEED_WORKSPACE_KEY": "android",
                "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Android",
                "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                "HERMES_SEED_AGENT_KEY": "android-agent",
                "HERMES_SEED_DEVICE_KEY": DEVICE,
            },
        )
        tenant_id, device_id, credential_id, agent_id = _activate_owner_control_device(
            database_url
        )

        gateway = build_production_connector_gateway_application(
            environment={
                "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_secret,
                "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                "HERMES_OWNER_CONTROL_SOCKET": str(socket_path),
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
            },
            application_factory=create_app,
        )
        business = build_production_business_api_application(
            environment={
                "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                "HERMES_SIGNING_SECRET_FILE": business_secret,
                "HERMES_OWNER_CONTROL_SOCKET": str(socket_path),
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
            },
        )
        now = int(datetime.now(UTC).timestamp())
        connector_token = jwt.encode(
            {
                "tenant_id": str(tenant_id),
                "device_id": str(device_id),
                "credential_id": str(credential_id),
                "agent_id": str(agent_id),
                "scopes": [
                    "session.observe",
                    "session.control.request",
                ],
                "jti": str(uuid4()),
                "iat": now,
                "nbf": now,
                "exp": now + 300,
            },
            connector_secret_value,
            algorithm="HS256",
            headers={"typ": "JWT"},
        )

        with (
            TestClient(gateway, base_url="https://gateway") as gateway_client,
            TestClient(business, base_url="https://business") as business_client,
        ):
            login = business_client.post(
                "/auth/password-login",
                json={
                    "provider": "basic",
                    "username": "android-user",
                    "password": "correct-password",
                    "next": "",
                },
            )
            assert login.json() == {"ok": True}
            browser_headers = {
                "Accept": "application/json",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            }
            empty_catalog = business_client.get(
                f"/api/v1/agents/{agent_id}/sessions",
                params={"min_messages": 0},
                headers=browser_headers,
            )
            assert empty_catalog.status_code == 200
            assert empty_catalog.json()["sessions"] == []

            with gateway_client.websocket_connect(
                "/api/ws",
                headers={
                    "Authorization": f"Bearer {connector_token}",
                },
                subprotocols=["hermes.connector.v1"],
            ) as connector:
                connector.send_json(
                    _hello(
                        tenant_id=tenant_id,
                        device_id=device_id,
                    )
                )
                welcome = connector.receive_json()
                assert welcome["payload"]["accepted_capabilities"] == [
                    "session.catalog.v1",
                    "session.observe",
                    "session.control",
                ]
                connector.send_json(
                    _catalog_snapshot(
                        tenant_id=tenant_id,
                        device_id=device_id,
                    )
                )
                catalog_ack = connector.receive_json()
                assert catalog_ack["message_type"] == "session.catalog.ack"
                assert catalog_ack["payload"]["ack_kind"] == "snapshot_committed"
                catalog = business_client.get(
                    f"/api/v1/agents/{agent_id}/sessions",
                    params={"min_messages": 0},
                    headers=browser_headers,
                )
                assert catalog.status_code == 200
                stable_session_id = catalog.json()["sessions"][0]["id"]
                operations: list[str] = []
                errors: list[BaseException] = []
                responder = threading.Thread(
                    target=_serve_owner_control,
                    args=(connector,),
                    kwargs={
                        "tenant_id": tenant_id,
                        "device_id": device_id,
                        "operations": operations,
                        "errors": errors,
                    },
                    daemon=True,
                )
                responder.start()

                ticket = _control_ticket(
                    business_client,
                    session_id=stable_session_id,
                    agent_id=agent_id,
                )
                with business_client.websocket_connect(
                    f"/api/ws?ticket={ticket}",
                    subprotocols=["hermes.tui.v1"],
                ) as control:
                    ready = control.receive_json()
                    assert (
                        ready["params"]["payload"]["control_available_methods"]
                        == CONTROL_METHODS
                    ), (ready, operations, errors)
                    control.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "session.control.acquire",
                            "params": {
                                "session_id": stable_session_id,
                            },
                        }
                    )
                    acquire_response = control.receive_json()
                    assert "result" in acquire_response, acquire_response
                    assert acquire_response["result"]["lease_id"] == LEASE_ID
                    control.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session.control.status",
                            "params": {
                                "session_id": stable_session_id,
                            },
                        }
                    )
                    assert (
                        control.receive_json()["result"]["controller_kind"] == "mobile"
                    )

                responder.join(timeout=2)
                assert not responder.is_alive()
                assert errors == []
                assert operations == [
                    "control.transport.open",
                    "session.control.acquire",
                    "session.control.status",
                    "control.transport.close",
                ]

            unavailable_ticket = _control_ticket(
                business_client,
                session_id=stable_session_id,
                agent_id=agent_id,
            )
            with business_client.websocket_connect(
                f"/api/ws?ticket={unavailable_ticket}",
                subprotocols=["hermes.tui.v1"],
            ) as unavailable:
                assert (
                    unavailable.receive_json()["params"]["payload"][
                        "control_available_methods"
                    ]
                    == []
                )

        engine = build_sqlite_engine(database_url)
        try:
            with Session(engine) as session:
                assert (
                    session.scalar(
                        select(func.count()).select_from(ControlCommandModel)
                    )
                    == 0
                )
                projection = session.scalar(
                    select(SessionProjectionModel).where(
                        SessionProjectionModel.session_key == SESSION_KEY
                    )
                )
                assert projection is not None
                assert projection.agent_id is not None
        finally:
            engine.dispose()
