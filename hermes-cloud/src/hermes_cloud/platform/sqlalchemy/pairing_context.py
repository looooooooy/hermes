"""SQLAlchemy-backed authorized Desktop pairing context."""

from __future__ import annotations

from sqlalchemy import select

from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.device.onboarding_context import PairingTarget
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)

_MAX_PAIRING_TARGETS = 128


class SqlAlchemyPairingContextResolver:
    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: object) -> None:
        self._session_factory = session_factory

    def targets_for(self, principal: Principal) -> tuple[PairingTarget, ...]:
        with self._session_factory() as session:
            statement = (
                select(
                    WorkspaceModel.workspace_id,
                    WorkspaceModel.workspace_key,
                    WorkspaceModel.display_name,
                    AgentModel.agent_id,
                    AgentModel.agent_key,
                )
                .join(
                    WorkspaceMembershipModel,
                    (WorkspaceMembershipModel.tenant_id == WorkspaceModel.tenant_id)
                    & (WorkspaceMembershipModel.workspace_id == WorkspaceModel.workspace_id),
                )
                .join(
                    AgentModel,
                    (AgentModel.tenant_id == WorkspaceModel.tenant_id)
                    & (AgentModel.workspace_id == WorkspaceModel.workspace_id),
                )
                .where(
                    WorkspaceModel.tenant_id == principal.tenant_id,
                    WorkspaceModel.status == "active",
                    WorkspaceMembershipModel.user_id == principal.user_id,
                    WorkspaceMembershipModel.status == "active",
                    AgentModel.status == "active",
                )
                .order_by(
                    WorkspaceModel.workspace_key.asc(),
                    AgentModel.agent_key.asc(),
                )
                .limit(_MAX_PAIRING_TARGETS + 1)
            )
            rows = session.execute(statement).all()
        if len(rows) > _MAX_PAIRING_TARGETS:
            raise ValueError("pairing context exceeds bounded target count")
        return tuple(
            PairingTarget(
                workspace_id=str(row.workspace_id),
                workspace_key=row.workspace_key,
                workspace_display_name=row.display_name,
                agent_id=str(row.agent_id),
                agent_key=row.agent_key,
            )
            for row in rows
        )


__all__ = ["SqlAlchemyPairingContextResolver"]
