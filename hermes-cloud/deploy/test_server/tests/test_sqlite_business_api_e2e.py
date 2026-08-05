from __future__ import annotations

import json
import runpy
import tempfile
from base64 import b64encode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from hermes_cloud.adapters.business_api_runtime import (
    build_production_business_api_application,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    PasswordCredentialModel,
    RoleModel,
    SessionMessageProjectionModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogEntryModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
SEED_MODELS = (
    TenantModel,
    UserModel,
    RoleModel,
    WorkspaceModel,
    WorkspaceMembershipModel,
    AgentModel,
    PasswordCredentialModel,
    SessionProjectionModel,
    SessionMessageProjectionModel,
)


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
                        "keys": {"test-v1": b64encode(b"k" * 32).decode("ascii")},
                    }
                },
            }
        ),
    )


def _seed_environment(
    *,
    dsn_file: str,
    password_file: str,
) -> dict[str, str]:
    return {
        "HERMES_BOOTSTRAP_DSN_FILE": dsn_file,
        "HERMES_INITIAL_USER_PASSWORD_FILE": password_file,
        "HERMES_SEED_TENANT_SLUG": "android-test",
        "HERMES_SEED_TENANT_DISPLAY_NAME": "Android Test",
        "HERMES_SEED_USERNAME": "android-user",
        "HERMES_SEED_USER_DISPLAY_NAME": "Android User",
        "HERMES_SEED_WORKSPACE_KEY": "android",
        "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Android",
        "HERMES_SEED_AGENT_KEY": "seed-agent",
    }


def _authorization(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _seed_row_counts(database_url: str) -> tuple[int, ...]:
    engine = build_sqlite_engine(database_url)
    try:
        with Session(engine) as session:
            return tuple(
                int(
                    session.scalar(
                        select(func.count()).select_from(model),
                    )
                    or 0
                )
                for model in SEED_MODELS
            )
    finally:
        engine.dispose()


def _insert_catalog_projection_fixture(database_url: str) -> tuple[UUID, UUID]:
    """Install a post-seed catalog projection fixture for Business API reads."""
    engine = build_sqlite_engine(database_url)
    observed_at = datetime.now(UTC)
    retention_until = observed_at + timedelta(days=30)
    try:
        with Session(engine) as session, session.begin():
            tenant = session.scalars(
                select(TenantModel).where(TenantModel.slug == "android-test")
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
                    AgentModel.agent_key == "seed-agent",
                )
            ).one()
            session_id = uuid4()
            message_id = uuid4()
            session.add(
                SessionCatalogEntryModel(
                    tenant_id=tenant.tenant_id,
                    session_id=session_id,
                    workspace_id=workspace.workspace_id,
                    agent_id=agent.agent_id,
                    profile="default",
                    session_key="android-bootstrap",
                    surface="hermes-cli",
                    authority_revision=1,
                    available_actions=["prompt.submit", "session.interrupt"],
                    runtime_generation="business-api-e2e",
                    writer_id=uuid4(),
                    writer_fence=1,
                    content_digest="a" * 64,
                    active=True,
                    updated_at=observed_at,
                )
            )
            session.add(
                SessionProjectionModel(
                        tenant_id=tenant.tenant_id,
                        session_id=session_id,
                        session_key="android-bootstrap",
                        workspace_id=workspace.workspace_id,
                        agent_id=agent.agent_id,
                        profile="default",
                        title="Hermes Cloud test session",
                        state="active",
                        revision=1,
                        lineage_tip_message_id=message_id,
                        lineage_tip_sequence=1,
                        started_at=observed_at,
                        updated_at=observed_at,
                        closed_at=None,
                        retention_until=retention_until,
                    )
            )
            session.flush()
            session.add(
                SessionMessageProjectionModel(
                        tenant_id=tenant.tenant_id,
                        session_id=session_id,
                        message_id=message_id,
                        sequence=1,
                        role="assistant",
                        content={"text": "Hermes Cloud is connected."},
                        parent_message_id=None,
                        created_at=observed_at,
                        retention_until=retention_until,
                    )
            )
            return UUID(str(agent.agent_id)), session_id
    finally:
        engine.dispose()


def test_sqlite_migrate_seed_login_refresh_and_projection_survive_reopen() -> None:
    migrate_main = runpy.run_path(
        str(SCRIPTS / "migrate_sqlite.py"),
        run_name="hermes_cloud_sqlite_e2e_migration",
    )["main"]
    seed_main = runpy.run_path(
        str(SCRIPTS / "seed_test_data.py"),
        run_name="hermes_cloud_sqlite_e2e_seed",
    )["main"]

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        database = directory / "hermes-cloud.db"
        database_url = f"sqlite+pysqlite:///{database}"
        migration_dsn = _write_private(directory / "migration-dsn", database_url)
        bootstrap_dsn = _write_private(directory / "bootstrap-dsn", database_url)
        runtime_dsn = _write_private(directory / "runtime-dsn", database_url)
        initial_password = _write_private(
            directory / "initial-password",
            "correct-password",
        )
        signing_secret = _write_private(
            directory / "business-signing",
            "sqlite-e2e-signing-key-material-with-at-least-32-bytes",
        )
        connector_signing_secret = _write_private(
            directory / "connector-signing",
            "sqlite-e2e-connector-signing-material-at-least-32-bytes",
        )
        observer_keyring = _observer_keyring(directory / "observer-keyring.json")

        migrate_main(
            [],
            environment={
                "HERMES_MIGRATION_DSN_FILE": migration_dsn,
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
            },
        )
        assert not database.exists()

        migrate_main(
            ["--apply"],
            environment={
                "HERMES_MIGRATION_DSN_FILE": migration_dsn,
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
            },
        )
        seed_environment = _seed_environment(
            dsn_file=bootstrap_dsn,
            password_file=initial_password,
        )
        seed_username = seed_environment["HERMES_SEED_USERNAME"]
        counts_before_seed = _seed_row_counts(database_url)
        assert counts_before_seed == (0,) * len(SEED_MODELS)
        seed_main(
            [],
            environment=seed_environment,
        )
        assert _seed_row_counts(database_url) == counts_before_seed

        seed_main(
            ["--apply"],
            environment=seed_environment,
        )
        assert _seed_row_counts(database_url) == (1,) * 7 + (0, 0)
        assert database.stat().st_mode & 0o777 == 0o660
        seed_agent_id, default_session_id = _insert_catalog_projection_fixture(
            database_url
        )
        engine = build_sqlite_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                default_projection = session.scalars(
                    select(SessionProjectionModel).where(
                        SessionProjectionModel.profile == "default"
                    )
                ).one()
                session.add(
                    SessionProjectionModel(
                        tenant_id=default_projection.tenant_id,
                        session_id=uuid4(),
                        session_key=default_projection.session_key,
                        workspace_id=default_projection.workspace_id,
                        agent_id=default_projection.agent_id,
                        profile="work",
                        title="Hermes Cloud work session",
                        state="active",
                        revision=1,
                        lineage_tip_message_id=uuid4(),
                        lineage_tip_sequence=1,
                        started_at=default_projection.started_at,
                        updated_at=default_projection.updated_at,
                        closed_at=None,
                        retention_until=default_projection.retention_until,
                    )
                )
        finally:
            engine.dispose()

        runtime_environment = {
            "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
            "HERMES_SIGNING_SECRET_FILE": signing_secret,
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing_secret,
            "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
        }
        first_application = build_production_business_api_application(
            environment=runtime_environment,
        )
        with TestClient(
            first_application,
            base_url="https://testserver",
        ) as client:
            assert client.get("/ready").status_code == 200
            login = client.post(
                "/auth/password-login",
                json={
                    "provider": "basic",
                    "username": seed_username,
                    "password": "correct-password",
                    "next": "",
                },
            )
            assert login.status_code == 200
            offer = client.post(
                "/api/device-pairing/offers",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "connector_instance_id": str(uuid4()),
                    "display_name": "SQLite E2E Connector",
                    "platform_family": "linux",
                    "connector_version": "0.1.0",
                    "key_algorithm": "Ed25519",
                    "public_key": urlsafe_b64encode(bytes(range(32)))
                    .rstrip(b"=")
                    .decode("ascii"),
                },
            )
            assert offer.status_code == 201, offer.text
            refresh = client.post(
                "/auth/native/refresh",
                json={
                    "refresh_token": login.cookies["hermes_session_rt"],
                    "provider": "basic",
                },
            )
            assert refresh.status_code == 200
            refresh_body = refresh.json()
            persisted_refresh_token = refresh_body["refresh_token"]
            catalog = client.get(
                f"/api/v1/agents/{seed_agent_id}/sessions",
                params={"min_messages": 0},
                headers=_authorization(refresh_body["access_token"]),
            )
            assert catalog.status_code == 200
            stable_session_id = catalog.json()["sessions"][0]["id"]
            control_ticket = client.post(
                "/api/auth/ws-ticket",
                json={
                    "connection_role": "control",
                    "client_instance_id": str(uuid4()),
                    "session_id": stable_session_id,
                    "agent_id": str(seed_agent_id),
                },
                headers=_authorization(refresh_body["access_token"]),
            )
            assert control_ticket.status_code == 200, control_ticket.text
            unknown_session_ticket = client.post(
                "/api/auth/ws-ticket",
                json={
                    "connection_role": "control",
                    "client_instance_id": str(uuid4()),
                    "session_id": str(uuid4()),
                    "agent_id": str(seed_agent_id),
                },
                headers=_authorization(refresh_body["access_token"]),
            )
            assert unknown_session_ticket.status_code == 404
            ticket_response = client.post(
                "/api/auth/ws-ticket",
                json={},
                headers=_authorization(refresh_body["access_token"]),
            )
            assert ticket_response.status_code == 200
            ticket = ticket_response.json()["ticket"]
            with client.websocket_connect(
                f"/api/ws?ticket={ticket}",
                subprotocols=["hermes.tui.v1"],
            ) as websocket:
                assert websocket.accepted_subprotocol == "hermes.tui.v1"
                assert websocket.receive_json()["params"]["type"] == "gateway.ready"
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(
                    f"/api/ws?ticket={ticket}",
                    subprotocols=["hermes.tui.v1"],
                ),
            ):
                pass

        reopened_application = build_production_business_api_application(
            environment=runtime_environment,
        )
        with TestClient(
            reopened_application,
            base_url="https://testserver",
        ) as client:
            persisted_refresh = client.post(
                "/auth/native/refresh",
                json={
                    "refresh_token": persisted_refresh_token,
                    "provider": "basic",
                },
            )
            assert persisted_refresh.status_code == 200
            authorization = _authorization(
                persisted_refresh.json()["access_token"],
            )
            ambiguous_page = client.get(
                "/api/sessions",
                params={
                    "limit": 20,
                    "offset": 0,
                    "min_messages": 1,
                    "archived": "exclude",
                    "order": "recent",
                },
                headers=authorization,
            )
            assert ambiguous_page.status_code == 409
            assert ambiguous_page.json() == {
                "code": "SESSION_SCOPE_AMBIGUOUS",
                "reason": "session scope is ambiguous",
            }
            page = client.get(
                "/api/sessions",
                params={
                    "limit": 20,
                    "offset": 0,
                    "min_messages": 1,
                    "archived": "exclude",
                    "order": "recent",
                    "profile": "default",
                    "agent_id": str(seed_agent_id),
                },
                headers=authorization,
            )
            assert page.status_code == 200
            assert page.json()["total"] == 1
            session = page.json()["sessions"][0]
            assert session["_lineage_root_id"] == str(default_session_id)
            assert session["title"] == "Hermes Cloud test session"

            work_page = client.get(
                "/api/sessions",
                params={
                    "limit": 20,
                    "offset": 0,
                    "min_messages": 1,
                    "archived": "exclude",
                    "order": "recent",
                    "profile": "work",
                    "agent_id": str(seed_agent_id),
                },
                headers=authorization,
            )
            assert work_page.status_code == 200
            assert work_page.json()["total"] == 1
            assert work_page.json()["sessions"][0]["profile"] == "work"

            detail = client.get(
                f"/api/v1/agents/{seed_agent_id}/sessions/{stable_session_id}",
                params={"profile": "default"},
                headers=authorization,
            )
            assert detail.status_code == 200
            assert detail.json()["id"] == stable_session_id
            assert detail.json()["directory_source"] == "host_catalog"

            transcript = client.get(
                (
                    f"/api/v1/agents/{seed_agent_id}/sessions/"
                    f"{stable_session_id}/messages"
                ),
                params={"limit": 200, "offset": 0, "profile": "default"},
                headers=authorization,
            )
            assert transcript.status_code == 200
            assert transcript.json()["pagination"]["returned"] == 1
            assert transcript.json()["messages"][0]["content"] == {
                "text": "Hermes Cloud is connected."
            }
