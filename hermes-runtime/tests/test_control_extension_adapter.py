from hermes_runtime.control.control_extension_adapter import (
    ControlActionRequest,
    ControlExtensionAdapter,
)
from hermes_runtime.control.event_queue import RuntimeEventQueue
from hermes_runtime.control.runtime_event import RuntimeEventState
from hermes_runtime.control.session_authority import SessionAuthority, SessionBinding


class FakeSession:
    def interrupt(self):
        pass

    def resume(self):
        pass

    def approve(self, payload):
        pass


def test_control_action_becomes_queued_runtime_event():
    authority = SessionAuthority()
    authority.bind(SessionBinding("s-1", "g-1", "default", FakeSession()))
    queue = RuntimeEventQueue()
    adapter = ControlExtensionAdapter(
        session_authority=authority,
        event_queue=queue,
    )

    returned = adapter.dispatch(
        ControlActionRequest(
            command_id="cmd-1",
            runtime_generation="g-1",
            session_id="s-1",
            action="interrupt",
            payload={},
        )
    )

    queued = queue.pop()
    assert returned.state is RuntimeEventState.QUEUED
    assert queued == returned
