from hermes_runtime.control.control_extension_adapter import (
    ControlActionRequest,
    ControlExtensionAdapter,
)


class FakeSessionAuthority:
    def resolve(self, session_id, runtime_generation):
        return type("Session", (), {"session_id": session_id})()


class FakeQueue:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)


def test_control_action_becomes_runtime_event():
    queue = FakeQueue()
    adapter = ControlExtensionAdapter(
        session_authority=FakeSessionAuthority(),
        event_queue=queue,
    )

    event = adapter.dispatch(
        ControlActionRequest(
            command_id="cmd-1",
            runtime_generation="g-1",
            session_id="s-1",
            action="interrupt",
            payload={},
        )
    )

    assert event.command_id == "cmd-1"
    assert queue.events[0] == event
