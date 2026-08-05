"""Real SQLite-backed Cloud ASGI process for H5 cross-component tests."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from hermes_cloud.entrypoints.business_api import create_app
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings
from hermes_cloud.modules.identity.domain import (
    Argon2PasswordHasher,
    PasswordCredential,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    RoleModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog import (
    SqlAlchemySessionCatalogRepository,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogEntryModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteLoginTenantResolver,
    SQLiteOperationScopedIdentityRepository,
    SQLiteOperationScopedSessionProjectionRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata
from sqlalchemy.orm import Session, sessionmaker

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
SESSION_ID = UUID("88888888-8888-4888-8888-888888888888")
SIGNING_KEY = b"real-h5-cloud-integration-signing-key-32-bytes"


class _SecretResolver:
    def resolve(self, reference: str) -> bytes:
        if reference != "secret-manager/test/real-h5-cloud":
            raise KeyError(reference)
        return SIGNING_KEY


def _seed(factory: sessionmaker[Session], now: datetime) -> None:
    role_id = uuid4()
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="real-h5-cloud",
                display_name="Real H5 Cloud",
                status="active",
                created_at=now,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                subject="operator@example.test",
                display_name="H5 Operator",
                email=None,
                status="active",
                created_at=now,
            )
        )
        session.add(
            RoleModel(
                tenant_id=TENANT_ID,
                role_id=role_id,
                role_key="workspace-member",
                display_name="Workspace Member",
                scope_type="workspace",
                permissions=[],
                status="active",
                version=1,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            WorkspaceModel(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                workspace_key="real-h5-cloud",
                display_name="Real H5 Cloud",
                status="active",
                created_by=USER_ID,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            AgentModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                agent_key="integration-agent",
                status="active",
                last_seen_at=now,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=TENANT_ID,
                workspace_membership_id=uuid4(),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                role_id=role_id,
                status="active",
                joined_at=now,
                revoked_at=None,
            )
        )

    identities = SQLiteOperationScopedIdentityRepository(factory)
    identities.store_password_credential(
        PasswordCredential(
            tenant_id=TENANT_ID,
            credential_id=uuid4(),
            user_id=USER_ID,
            subject="operator@example.test",
            password_hash=Argon2PasswordHasher().hash("correct-password"),
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    with factory.begin() as session:
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=SESSION_ID,
                session_key="real-session-1",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="",
                state="active",
                revision=0,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=now,
                updated_at=now,
                closed_at=None,
                retention_until=now + timedelta(days=3650),
            )
        )
        session.add(
            SessionCatalogEntryModel(
            tenant_id=TENANT_ID,
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_ID,
            agent_id=AGENT_ID,
            profile="default",
            session_key="real-session-1",
            surface="terminal",
            authority_revision=1,
            available_actions=["prompt.submit", "session.interrupt"],
            runtime_generation="integration-generation-1",
            writer_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            writer_fence=1,
            content_digest=sha256(b"real-h5-post-catalog-fixture").hexdigest(),
            active=True,
            updated_at=now,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", type=Path, required=True)
    arguments = parser.parse_args()
    database = arguments.database.resolve()
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{database}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    database.chmod(0o660)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    _seed(factory, now)
    application = create_app(
        identity_repository=SQLiteOperationScopedIdentityRepository(factory),
        projection_repository=SQLiteOperationScopedSessionProjectionRepository(
            factory
        ),
        session_catalog_repository=SqlAlchemySessionCatalogRepository(factory),
        tenant_resolver=SQLiteLoginTenantResolver(factory),
        secret_resolver=_SecretResolver(),
        settings=CloudApiSettings(
            signing_secret_ref="secret-manager/test/real-h5-cloud",
            access_ttl_seconds=300,
            refresh_ttl_seconds=3600,
            ticket_ttl_seconds=60,
        ),
        now=lambda: datetime.now(UTC),
    )
    try:
        uvicorn.run(
            application,
            host="127.0.0.1",
            port=arguments.port,
            access_log=False,
            log_level="warning",
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
