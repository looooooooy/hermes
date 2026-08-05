"""ORM-only resolution from an authorized session to one Connector route."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
from typing import Protocol

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceLifecycleModel,
    DeviceModel,
    SessionProjectionModel,
    TenantModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


class SqlAlchemyControlRouteResolver:
    """Fail closed unless exactly one active device owns the visible session."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        return await asyncio.to_thread(self._resolve_sync, context)

    def _resolve_sync(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        authentication = context.authentication
        principal = authentication.principal
        if authentication.agent_id is None:
            raise LookupError("authorized control route is unavailable")
        statement = (
            select(TenantModel.tenant_id, DeviceModel.device_id)
            .join(
                SessionProjectionModel,
                SessionProjectionModel.tenant_id == TenantModel.tenant_id,
            )
            .join(
                WorkspaceModel,
                and_(
                    WorkspaceModel.tenant_id == SessionProjectionModel.tenant_id,
                    WorkspaceModel.workspace_id == SessionProjectionModel.workspace_id,
                ),
            )
            .join(
                WorkspaceMembershipModel,
                and_(
                    WorkspaceMembershipModel.tenant_id
                    == SessionProjectionModel.tenant_id,
                    WorkspaceMembershipModel.workspace_id
                    == SessionProjectionModel.workspace_id,
                ),
            )
            .join(
                AgentModel,
                and_(
                    AgentModel.tenant_id == SessionProjectionModel.tenant_id,
                    AgentModel.agent_id == SessionProjectionModel.agent_id,
                    AgentModel.workspace_id == SessionProjectionModel.workspace_id,
                ),
            )
            .join(
                DeviceModel,
                and_(
                    DeviceModel.tenant_id == SessionProjectionModel.tenant_id,
                    DeviceModel.agent_id == AgentModel.agent_id,
                    DeviceModel.workspace_id == SessionProjectionModel.workspace_id,
                ),
            )
            .join(
                DeviceLifecycleModel,
                and_(
                    DeviceLifecycleModel.tenant_id == SessionProjectionModel.tenant_id,
                    DeviceLifecycleModel.device_id == DeviceModel.device_id,
                    DeviceLifecycleModel.workspace_id
                    == SessionProjectionModel.workspace_id,
                    DeviceLifecycleModel.agent_id == AgentModel.agent_id,
                ),
            )
            .where(
                SessionProjectionModel.tenant_id == principal.tenant_id,
                SessionProjectionModel.session_id == authentication.session_id,
                SessionProjectionModel.session_key == authentication.session_key,
                SessionProjectionModel.agent_id == authentication.agent_id,
                SessionProjectionModel.profile == authentication.profile,
                AgentModel.agent_id == authentication.agent_id,
                WorkspaceMembershipModel.user_id == principal.user_id,
                WorkspaceMembershipModel.status == "active",
                TenantModel.status == "active",
                WorkspaceModel.status == "active",
                AgentModel.status == "active",
                DeviceModel.status == "active",
                DeviceLifecycleModel.state == "active",
            )
            .distinct()
            .limit(2)
        )
        with self._session_factory.begin() as session:
            routes = session.execute(statement).all()
        if len(routes) != 1:
            raise LookupError("authorized control route is unavailable")
        tenant_id, device_id = routes[0]
        return ControlConnectorRoute(
            tenant_id=str(tenant_id),
            device_id=str(device_id),
            principal_tenant_id=str(principal.tenant_id),
        )
