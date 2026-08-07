from hermes_runtime.control.agent_turn_consumer import AgentTurnConsumer


class FakeQueue:
    def __init__(self, event):
        self.event = event

    def pop(self):
        value = self.event
        self.event = None
        return value


class FakeLoop:
    def handle_event(self, event):
        return "ok"


class Event:
    event_id = "evt-1"
    session_id = "session-1"


def test_consumer_forwards_event_to_agent_loop():
    result = AgentTurnConsumer(FakeQueue(Event()), FakeLoop()).consume_once()

    assert result is not None
    assert result.state == "completed"
    assert result.session_id == "session-1"
