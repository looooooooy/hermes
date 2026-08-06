from hermes_runtime.control.event_consumer import RuntimeEventConsumer


class FakeQueue:
    def __init__(self, event):
        self.event = event

    def pop(self):
        value = self.event
        self.event = None
        return value


def test_consumer_returns_effect_receipt():
    event = FakeEvent()
    consumer = RuntimeEventConsumer(
        FakeQueue(event),
        lambda _: "ok",
    )

    receipt = consumer.consume_once()

    assert receipt is not None
    assert receipt.state == "completed"


class FakeEvent:
    event_id = "evt-1"
    command_id = "cmd-1"

    def processing(self):
        pass

    def completed(self):
        pass

    def failed(self, _error):
        pass
