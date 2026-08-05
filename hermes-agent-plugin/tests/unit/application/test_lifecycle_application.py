"""Canonical lifecycle application tests."""

from pathlib import Path


class _Resource:
    name = "recording"

    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self, _deadline: float) -> None:
        self.events.append("start")

    def drain(self, _deadline: float) -> None:
        self.events.append("drain")

    def stop(self, _deadline: float) -> None:
        self.events.append("stop")


class _Handshake:
    def handle_hello(self, raw: object) -> str:
        return f"welcome:{raw}"


def test_canonical_lifecycle_runs() -> None:
    module_path = (
        Path(__file__).parents[3] / "src/hermes_agent_plugin/application/lifecycle.py"
    )
    assert module_path.is_file(), "canonical lifecycle application is missing"

    from hermes_agent_plugin.application.lifecycle import GatewayLifecycle
    from hermes_agent_plugin.domain.lifecycle import GatewayState

    resource = _Resource()
    lifecycle = GatewayLifecycle(
        resources=[resource],
        adapter_factory=lambda _generation: _Handshake(),
        generation_factory=lambda: "runtime-1",
        clock=lambda: 100.0,
    )

    lifecycle.install()
    assert lifecycle.start() == "runtime-1"
    assert lifecycle.handle_local_hello("hello") == "welcome:hello"
    lifecycle.stop()

    assert lifecycle.state is GatewayState.STOPPED
    assert resource.events == ["start", "drain", "stop"]
