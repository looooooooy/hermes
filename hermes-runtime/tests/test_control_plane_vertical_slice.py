import pytest

from hermes_runtime.control.command_receipt_bridge import CommandReceiptBridge
from hermes_runtime.control.remote_control_flow import RemoteControlFlowCoordinator
from hermes_runtime.control.runtime_control_service import (
    RuntimeControlRequest,
    RuntimeControlService,
)
from hermes_runtime.control.session_action_router import SessionActionRouter
from hermes_runtime.control.session_authority import SessionAuthority, SessionBinding


class RecordingSession:
    def __init__(self):
        self.actions = []

    def interrupt(self):
        self.actions.append(("interrupt", None))

    def resume(self):
        self.actions.append(("resume", None))

    def approve(self, payload):
        self.actions.append(("approve", dict(payload)))


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("interrupt", {}),
        ("resume", {}),
        ("approve", {"pending_request_id": "pending-1", "decision": "allow"}),
    ],
)
def test_runtime_control_vertical_slice(action, payload):
    session = RecordingSession()
    authority = SessionAuthority()
    authority.bind(SessionBinding("s-1", "g-1", "default", session))
    receipts = CommandReceiptBridge()
    service = RuntimeControlService(
        RemoteControlFlowCoordinator(authority, SessionActionRouter(), receipts)
    )

    result = service.dispatch(
        RuntimeControlRequest(
            command_id=f"cmd-{action}",
            runtime_generation="g-1",
            session_id="s-1",
            action=action,
            payload=payload,
        )
    )

    assert result.state == "completed"
    assert session.actions[0][0] == action
    assert receipts.get(f"cmd-{action}").state == "completed"
