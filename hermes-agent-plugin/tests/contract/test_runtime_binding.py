from hermes_agent_plugin.runtime_binding import (
    ExtensionRegistry,
    ExtensionState,
    ExtensionStatus,
    RuntimeBinding,
)


def test_extension_registration_and_ready_state():
    registry = ExtensionRegistry()
    registry.register(
        ExtensionStatus(
            name="control",
            version="1",
            capabilities={"session.control"},
        )
    )

    runtime = RuntimeBinding(
        runtime_id="runtime-1",
        runtime_generation="generation-1",
        profile="default",
    )

    registry.mark_ready("control", runtime)

    snapshot = registry.snapshot()[0]
    assert snapshot["state"] == ExtensionState.READY.value
    assert snapshot["runtime_generation"] == "generation-1"
    assert "session.control" in snapshot["capabilities"]
