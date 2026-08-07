from __future__ import annotations

from hermes_agent_plugin.extension_runtime_bridge import ExtensionRuntimeBridge
from hermes_agent_plugin.runtime_binding import RuntimeBinding


def test_extension_runtime_bridge_registers_and_publishes_ready_state() -> None:
    bridge = ExtensionRuntimeBridge(
        name="control",
        version="1",
        capabilities={"session.control"},
    )

    bridge.register()
    bridge.ready(
        RuntimeBinding(
            runtime_id="runtime-1",
            runtime_generation="generation-1",
            profile="default",
        )
    )

    snapshot = bridge.snapshot()

    assert snapshot["name"] == "control"
    assert snapshot["state"] == "ready"
    assert snapshot["runtime_generation"] == "generation-1"
    assert snapshot["capabilities"] == ["session.control"]
