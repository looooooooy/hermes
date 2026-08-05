from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.postgres.models import (
    SessionProjectionModel,
    WebSocketTicketModel,
)


def test_postgres_v11_publishes_fail_closed_durable_session_identity_plan() -> None:
    migration = POSTGRES_V1_MIGRATIONS[10]
    keys = tuple(operation.key for operation in migration.plan.operations)

    assert migration.version == 11
    assert migration.name == "0011_session_projection_durable_identity"
    assert keys == (
        "assert-empty:session-identity-v10",
        "column:projection.sessions.profile:add",
        "column:identity.websocket_tickets.session_id:add",
        "constraint:projection.sessions.tenant-session-key:drop",
        "constraint:identity.websocket_tickets.tenant-session-key:drop",
        "column:projection.sessions.profile:not-null",
        "column:identity.websocket_tickets.session_key:drop",
        "constraint:projection.sessions.durable-identity:add",
        "constraint:identity.websocket_tickets.stable-session:add",
        "index:session_projection_identity_idx",
        "index:session_projection_legacy_identity_uq",
    )
    assert all(not hasattr(operation.statement({}), "text") for operation in migration.plan.operations)


def test_v11_current_models_expose_stable_ticket_and_four_tuple_identity() -> None:
    session_table = SessionProjectionModel.__table__
    ticket_table = WebSocketTicketModel.__table__

    assert session_table.c.profile.nullable is False
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns)
        == ("tenant_id", "agent_id", "profile", "session_key")
        for constraint in session_table.constraints
    )
    assert {index.name for index in session_table.indexes} >= {
        "session_projection_identity_idx",
        "session_projection_legacy_identity_uq",
    }
    assert "session_id" in ticket_table.c
    assert "session_key" not in ticket_table.c
    assert any(
        isinstance(constraint, ForeignKeyConstraint)
        and tuple(column.name for column in constraint.columns)
        == ("tenant_id", "session_id")
        for constraint in ticket_table.constraints
    )
