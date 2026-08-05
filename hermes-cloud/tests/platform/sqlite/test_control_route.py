from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.modules.cloud_api.domain import (
    Principal,
    WebSocketTicketAuthentication,
)
from hermes_cloud.modules.control.domain import ControlRequestContext
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceLifecycleModel,
    DeviceModel,
    RoleModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.control_route import (
    SqlAlchemyControlRouteResolver,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
ROLE_ID = UUID("33333333-3333-4333-8333-333333333333")
SESSION_WORKSPACE_ID = UUID("44444444-4444-4444-8444-444444444444")
OTHER_WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
DEVICE_ID = UUID("77777777-7777-4777-8777-777777777777")
SECOND_DEVICE_ID = UUID("77777777-7777-4777-8777-777777777778")
SESSION_ID = UUID("88888888-8888-4888-8888-888888888888")


def _context() -> ControlRequestContext:
    return ControlRequestContext(
        authentication=WebSocketTicketAuthentication(
            principal=Principal(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                provider="basic",
                refresh_session_id=UUID("99999999-9999-4999-8999-999999999999"),
            ),
            connection_role="control",
            client_instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            session_id=SESSION_ID,
            session_key="workspace-bound-session",
            profile="default",
            agent_id=AGENT_ID,
        ),
        connection_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )


def _seed_cross_workspace_route(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="workspace-route",
                display_name="Workspace Route",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                subject="workspace-owner",
                display_name="Workspace Owner",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            RoleModel(
                tenant_id=TENANT_ID,
                role_id=ROLE_ID,
                role_key="owner",
                display_name="Owner",
                scope_type="workspace",
                permissions=[],
                status="active",
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            (
                WorkspaceModel(
                    tenant_id=TENANT_ID,
                    workspace_id=SESSION_WORKSPACE_ID,
                    workspace_key="session-workspace",
                    display_name="Session Workspace",
                    status="active",
                    created_by=USER_ID,
                    created_at=NOW,
                ),
                WorkspaceModel(
                    tenant_id=TENANT_ID,
                    workspace_id=OTHER_WORKSPACE_ID,
                    workspace_key="other-workspace",
                    display_name="Other Workspace",
                    status="active",
                    created_by=USER_ID,
                    created_at=NOW,
                ),
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=TENANT_ID,
                workspace_membership_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                workspace_id=SESSION_WORKSPACE_ID,
                user_id=USER_ID,
                role_id=ROLE_ID,
                status="active",
                joined_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            AgentModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                workspace_id=SESSION_WORKSPACE_ID,
                agent_key="shared-agent",
                status="active",
                last_seen_at=NOW,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            DeviceModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                agent_id=AGENT_ID,
                workspace_id=OTHER_WORKSPACE_ID,
                device_key="other-workspace-device",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            DeviceLifecycleModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                workspace_id=OTHER_WORKSPACE_ID,
                agent_id=AGENT_ID,
                state="active",
                revision=1,
                updated_at=NOW,
            )
        )
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=SESSION_ID,
                session_key="workspace-bound-session",
                workspace_id=SESSION_WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="Workspace-bound session",
                state="active",
                revision=1,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=NOW,
                updated_at=NOW,
                closed_at=None,
                retention_until=NOW + timedelta(days=1),
            )
        )


def _build_resolver(
    tmp_path: Path,
) -> tuple[object, sessionmaker[Session], SqlAlchemyControlRouteResolver]:
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'control-route.sqlite3'}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _seed_cross_workspace_route(factory)
    return engine, factory, SqlAlchemyControlRouteResolver(factory)


def _align_device_with_session(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        device = session.get(DeviceModel, (TENANT_ID, DEVICE_ID))
        lifecycle = session.get(DeviceLifecycleModel, (TENANT_ID, DEVICE_ID))
        assert device is not None
        assert lifecycle is not None
        device.workspace_id = SESSION_WORKSPACE_ID
        lifecycle.workspace_id = SESSION_WORKSPACE_ID


def test_control_route_rejects_device_from_another_workspace(
    tmp_path: Path,
) -> None:
    engine, factory, resolver = _build_resolver(tmp_path)
    try:
        with pytest.raises(LookupError, match="route is unavailable"):
            asyncio.run(resolver.resolve(_context()))

        _align_device_with_session(factory)

        route = asyncio.run(resolver.resolve(_context()))
        assert route.tenant_id == str(TENANT_ID)
        assert route.device_id == str(DEVICE_ID)
        assert route.principal_tenant_id == str(TENANT_ID)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("model", "identity", "attribute", "value"),
    (
        (WorkspaceModel, (TENANT_ID, SESSION_WORKSPACE_ID), "status", "suspended"),
        (AgentModel, (TENANT_ID, AGENT_ID), "status", "disabled"),
        (
            AgentModel,
            (TENANT_ID, AGENT_ID),
            "workspace_id",
            OTHER_WORKSPACE_ID,
        ),
        (DeviceModel, (TENANT_ID, DEVICE_ID), "status", "disabled"),
        (
            DeviceLifecycleModel,
            (TENANT_ID, DEVICE_ID),
            "state",
            "suspended",
        ),
        (DeviceLifecycleModel, (TENANT_ID, DEVICE_ID), "state", "revoked"),
    ),
)
def test_control_route_requires_every_authority_link_to_be_active(
    tmp_path: Path,
    model: type[object],
    identity: tuple[UUID, UUID],
    attribute: str,
    value: object,
) -> None:
    engine, factory, resolver = _build_resolver(tmp_path)
    try:
        _align_device_with_session(factory)
        with factory.begin() as session:
            row = session.get(model, identity)
            assert row is not None
            setattr(row, attribute, value)

        with pytest.raises(LookupError, match="route is unavailable"):
            asyncio.run(resolver.resolve(_context()))
    finally:
        engine.dispose()


def test_control_route_rejects_ambiguous_active_device_chain(
    tmp_path: Path,
) -> None:
    engine, factory, resolver = _build_resolver(tmp_path)
    try:
        _align_device_with_session(factory)
        with factory.begin() as session:
            session.add(
                DeviceModel(
                    tenant_id=TENANT_ID,
                    device_id=SECOND_DEVICE_ID,
                    agent_id=AGENT_ID,
                    workspace_id=SESSION_WORKSPACE_ID,
                    device_key="second-active-device",
                    status="active",
                    created_at=NOW,
                )
            )
            session.flush()
            session.add(
                DeviceLifecycleModel(
                    tenant_id=TENANT_ID,
                    device_id=SECOND_DEVICE_ID,
                    workspace_id=SESSION_WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    state="active",
                    revision=1,
                    updated_at=NOW,
                )
            )

        with pytest.raises(LookupError, match="route is unavailable"):
            asyncio.run(resolver.resolve(_context()))
    finally:
        engine.dispose()
