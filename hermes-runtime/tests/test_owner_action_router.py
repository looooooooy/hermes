from hermes_runtime.control.event_queue import RuntimeEventQueue
from hermes_runtime.control.owner_action_router import (
    OwnerActionRequest,
    OwnerActionRouter,
)


def test_owner_action_routes_to_runtime_event_queue() -> None:
    queue = RuntimeEventQueue()
    router = OwnerActionRouter(queue)

    accepted = router.route(
        OwnerActionRequest(
            command_id="cmd-001",
            runtime_generation="runtime-001",
            action="interrupt",
            payload={"reason": "owner_request"},
        )
    )

    assert accepted is True
    event = queue.pop()
    assert event is not None
    assert event.command_id == "cmd-001"
    assert event.event_type == "interrupt"
