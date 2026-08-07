from hermes_cloud.runtime_identity.events import RuntimeIdentityEvent
from hermes_cloud.runtime_identity.rollover import rollover_runtime


def test_runtime_identity_event_creation() -> None:
    event = RuntimeIdentityEvent.create(
        runtime_id="rt-1",
        runtime_generation="gen-1",
        event_type="runtime.active",
    )
    assert event.runtime_id == "rt-1"
    assert event.event_type == "runtime.active"


def test_runtime_rollover_invalidates_old_generation() -> None:
    result = rollover_runtime("gen-1", "gen-2")
    assert result.invalidated is True
