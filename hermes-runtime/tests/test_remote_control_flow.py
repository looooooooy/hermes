import pytest

from hermes_runtime.control.command_receipt_bridge import CommandReceiptBridge
from hermes_runtime.control.remote_control_flow import (
    CommandConflict,
    RemoteControlFlowCoordinator,
)
from hermes_runtime.control.session_action_router import SessionActionRouter
from hermes_runtime.control.session_authority import SessionAuthority, SessionBinding


class FakeSession:
    def __init__(self):
        self.interrupt_count = 0
        self.resume_count = 0
        self.approvals = []

    def interrupt(self):
        self.interrupt_count += 1

    def resume(self):
        self.resume_count += 1

    def approve(self, payload):
        self.approvals.append(dict(payload))


def build_flow():
    session = FakeSession()
    authority = SessionAuthority()
    authority.bind(
        SessionBinding("session-001", "runtime-001", "default", session)
    )
    receipts = CommandReceiptBridge()
    flow = RemoteControlFlowCoordinator(
        authority,
        SessionActionRouter(),
        receipts,
    )
    return flow, session, receipts


def test_remote_control_flow_is_idempotent_and_publishes_receipt():
    flow, session, receipts = build_flow()

    first = flow.execute(
        command_id="cmd-001",
        runtime_generation="runtime-001",
        session_id="session-001",
        action="resume",
    )
    second = flow.execute(
        command_id="cmd-001",
        runtime_generation="runtime-001",
        session_id="session-001",
        action="resume",
    )

    assert first == second
    assert first.state == "completed"
    assert session.resume_count == 1
    assert receipts.get("cmd-001").state == "completed"


def test_command_id_conflict_fails_closed():
    flow, _session, _receipts = build_flow()
    flow.execute(
        command_id="cmd-001",
        runtime_generation="runtime-001",
        session_id="session-001",
        action="resume",
    )

    with pytest.raises(CommandConflict):
        flow.execute(
            command_id="cmd-001",
            runtime_generation="runtime-001",
            session_id="session-001",
            action="interrupt",
        )


def test_stale_runtime_generation_returns_stale_receipt():
    flow, session, receipts = build_flow()

    result = flow.execute(
        command_id="cmd-stale",
        runtime_generation="runtime-old",
        session_id="session-001",
        action="resume",
    )

    assert result.state == "stale"
    assert session.resume_count == 0
    assert receipts.get("cmd-stale").state == "stale"


def test_approval_requires_pending_request_id():
    flow, session, _receipts = build_flow()

    result = flow.execute(
        command_id="cmd-approve",
        runtime_generation="runtime-001",
        session_id="session-001",
        action="approve",
        payload={},
    )

    assert result.state == "rejected"
    assert session.approvals == []
