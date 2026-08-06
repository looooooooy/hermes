from hermes_runtime.control.agent_turn_queue import AgentTurnQueue
from hermes_runtime.control.runtime_control_service import (
    RuntimeControlRequest,
    RuntimeControlService,
)


def test_dispatch_creates_agent_turn_event():
    queue = AgentTurnQueue()
    service = RuntimeControlService(queue)

    event = service.dispatch(
        RuntimeControlRequest(
            command_id="cmd-001",
            session_id="session-001",
            action="resume",
            payload={"reason": "remote"},
        )
    )

    assert event.session_id == "session-001"
    assert queue.pop() == event
