from hermes_agent_plugin.extension_lifecycle import (
    ExtensionLifecycleCoordinator,
)
from hermes_agent_plugin.runtime_binding import RuntimeBinding


def test_extension_lifecycle_marks_ready():
    coordinator = ExtensionLifecycleCoordinator()
    coordinator.register_extension(
        "observer",
        "1",
        ["session.observe"],
    )

    result = coordinator.mark_ready(
        "observer",
        RuntimeBinding(
            runtime_id="runtime-1",
            runtime_generation="generation-1",
            profile="default",
        ),
    )

    assert result.ready is True
    assert result.state == "ready"


def test_extension_snapshot_contains_runtime_generation():
    coordinator = ExtensionLifecycleCoordinator()
    coordinator.register_extension("control", "1")
    coordinator.mark_ready(
        "control",
        RuntimeBinding(
            runtime_id="runtime-1",
            runtime_generation="generation-2",
            profile="work",
        ),
    )

    snapshot = coordinator.snapshot()

    assert snapshot[0]["runtime_generation"] == "generation-2"
