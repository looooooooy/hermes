from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (
    SqlAlchemyConnectorTransportCursorAuthority,
)


class _ZeroRowcountSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.statement = None

    def get(self, _model: object, _key: object, **_kwargs: object) -> object:
        return self.row

    def execute(self, statement: object) -> object:
        self.statement = statement
        return SimpleNamespace(rowcount=0)


class _SessionFactory:
    def __init__(self, session: _ZeroRowcountSession) -> None:
        self.session = session

    @contextmanager
    def begin(self):
        yield self.session


def test_postgres_resume_activation_uses_full_cas_and_rejects_zero_rowcount() -> None:
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    device_id = UUID("22222222-2222-4222-8222-222222222222")
    instance_id = UUID("33333333-3333-4333-8333-333333333333")
    old_connection = UUID("44444444-4444-4444-8444-444444444444")
    row = SimpleNamespace(
        tenant_id=tenant_id,
        device_id=device_id,
        connector_instance_id=instance_id,
        runtime_generation="runtime-generation-1",
        connection_id=old_connection,
        state="offline",
        next_connector_sequence=7,
        next_cloud_sequence=11,
        revision=9,
    )
    session = _ZeroRowcountSession(row)
    authority = SqlAlchemyConnectorTransportCursorAuthority(
        _SessionFactory(session),
        now=lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )
    ownership = SimpleNamespace(
        tenant_id=tenant_id,
        device_id=device_id,
        connector_instance_id=instance_id,
        runtime_generation="runtime-generation-1",
        connection_id=UUID("55555555-5555-4555-8555-555555555555"),
        previous_connection_id=old_connection,
        resume_decision="resumed",
        expected_next_connector_sequence=7,
        expected_next_cloud_sequence=11,
        next_connector_sequence=8,
        next_cloud_sequence=12,
    )

    with pytest.raises(RuntimeError, match="activation ownership changed"):
        authority._apply_activation(
            session,
            ownership=ownership,
            target_state="active",
            now=datetime(2026, 8, 1, tzinfo=UTC),
        )

    assert session.statement is not None
    compiled = str(session.statement.compile(dialect=postgresql.dialect()))
    for column in (
        "connection_id",
        "connector_instance_id",
        "runtime_generation",
        "next_connector_sequence",
        "next_cloud_sequence",
        "state",
        "revision",
    ):
        assert f"connector_transport_cursors.{column}" in compiled
