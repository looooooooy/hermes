from hermes_runtime.control.command_receipt_bridge import CommandReceiptBridge
from hermes_runtime.control.remote_control_flow import RemoteControlFlowCoordinator
from hermes_runtime.control.runtime_control_service import (
    RuntimeControlRequest,
    RuntimeControlService,
)
from hermes_runtime.control.session_action_router import SessionActionRouter
from hermes_runtime.control.session_authority import SessionAuthority, SessionBinding


class FakeSession:
    def __init__(self):
        self.resumed = False

    def interrupt(self):
        pass

    def resume(self):
        self.resumed = True

    def approve(self, payload):
        pass


def test_dispatch_uses_runtime_owned_session_controller():
    session = FakeSession()
    authority = SessionAuthority()
    authority.bind(SessionBinding("s-1", "g-1", "default", session))
    service = RuntimeControlService(
        RemoteControlFlowCoordinator(
            authority,
            SessionActionRouter(),
            CommandReceiptBridge(),
        )
    )

    result = service.dispatch(
        RuntimeControlRequest(
            command_id="cmd-001",
            runtime_generation="g-1",
            session_id="s-1",
            action="resume",
            payload={"reason": "remote"},
        )
    )

    assert result.state == "completed"
    assert session.resumed is True
