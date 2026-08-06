from hermes_runtime.control.event_consumer import RuntimeEventConsumer
from hermes_runtime.control.event_queue import RuntimeEventQueue
from hermes_runtime.control.runtime_event import RuntimeEvent, RuntimeEventState


def test_consumer_returns_effect_receipt_and_processing_event():
    queue = RuntimeEventQueue()
    event = RuntimeEvent.create(
        event_id="evt-1",
        command_id="cmd-1",
        runtime_generation="runtime-1",
        session_id="session-1",
        event_type="resume",
        payload={},
    )
    assert queue.enqueue(event) is True
    observed = []

    consumer = RuntimeEventConsumer(
        queue,
        lambda current: observed.append(current.state) or "ok",
    )

    receipt = consumer.consume_once()

    assert receipt is not None
    assert receipt.state == "completed"
    assert receipt.detail == "ok"
    assert receipt.runtime_generation == "runtime-1"
    assert observed == [RuntimeEventState.PROCESSING]


def test_consumer_redacts_effect_failure_details():
    queue = RuntimeEventQueue()
    queue.enqueue(
        RuntimeEvent.create(
            event_id="evt-2",
            command_id="cmd-2",
            runtime_generation="runtime-1",
            session_id="session-1",
            event_type="resume",
            payload={},
        )
    )

    def fail(_event):
        raise RuntimeError("secret tool output")

    receipt = RuntimeEventConsumer(queue, fail).consume_once()

    assert receipt is not None
    assert receipt.state == "failed"
    assert receipt.detail == "runtime_effect_failed"
