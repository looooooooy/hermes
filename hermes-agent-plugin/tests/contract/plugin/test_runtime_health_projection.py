from hermes_agent_plugin.runtime_binding import (
    ExtensionRegistry,
    ExtensionState,
    ExtensionStatus,
    RuntimeBinding,
)
from hermes_agent_plugin.runtime_health import RuntimeHealthProjector


def test_runtime_health_projection_reports_ready_extensions() -> None:
    registry = ExtensionRegistry()
    registry.register(
        ExtensionStatus(
            name="observer",
            version="1",
            capabilities={"session.observe"},
        )
    )

    runtime = RuntimeBinding(
        runtime_id="runtime-1",
        runtime_generation="generation-1",
        profile="default",
    )
    registry.mark_ready("observer", runtime)

    snapshot = RuntimeHealthProjector(registry).snapshot(runtime)

    assert snapshot.ready is True
    assert snapshot.extensions[0]["state"] == ExtensionState.READY.value
    assert snapshot.as_dict()["runtime"]["runtime_generation"] == "generation-1"
