from hermes_runtime.control.agent_event_adapter import AgentEventAdapter


class FakeSink:
    def __init__(self):
        self.events = []

    def submit_event(self, event):
        self.events.append(event)
        return event.event_id


def test_dispatch_creates_agent_runtime_event():
    sink = FakeSink()
    adapter = AgentEventAdapter(sink)

    result = adapter.dispatch(
        event_id="evt-1",
        session_id="session-1",
        runtime_generation="runtime-1",
        event_type="resume",
        payload={"source": "remote"},
    )

    assert result == "evt-1"
    assert sink.events[0].event_type == "resume"
    assert sink.events[0].session_id == "session-1"
