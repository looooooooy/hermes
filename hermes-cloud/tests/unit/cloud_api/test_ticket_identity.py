from __future__ import annotations

from uuid import UUID

from hermes_cloud.modules.cloud_api.adapters.http_tickets import (
    _parse_control_request,
)

SESSION_ID = "44444444-4444-4444-8444-444444444444"
AGENT_ID = "77777777-7777-4777-8777-777777777777"
CLIENT_ID = "33333333-3333-4333-8333-333333333333"


def test_control_ticket_accepts_only_stable_session_id_and_agent_scope() -> None:
    assert _parse_control_request(
        {
            "connection_role": "control",
            "client_instance_id": CLIENT_ID,
            "session_id": SESSION_ID,
            "agent_id": AGENT_ID,
        }
    ) == (CLIENT_ID, UUID(SESSION_ID), UUID(AGENT_ID))

    assert (
        _parse_control_request(
            {
                "connection_role": "control",
                "client_instance_id": CLIENT_ID,
                "session_key": "host-session-key",
                "profile": "default",
                "agent_id": AGENT_ID,
            }
        )
        is None
    )
