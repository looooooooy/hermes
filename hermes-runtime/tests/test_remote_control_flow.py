from hermes_runtime.control.remote_control_flow import (
    RemoteControlFlowCoordinator,
)


class FakeSessionAuthority:
    def resolve(self, session_id, runtime_generation):
        return {
            "session_id": session_id,
            "runtime_generation": runtime_generation,
        }


class FakeActionRouter:
    class Result:
        state = "completed"
        detail = "ok"

    def dispatch(self, **kwargs):
        return self.Result()


def test_remote_control_flow_executes_runtime_owned_action():
    coordinator = RemoteControlFlowCoordinator(
        FakeSessionAuthority(),
        FakeActionRouter(),
    )

    result = coordinator.execute(
        command_id="cmd-001",
        runtime_generation="runtime-001",
        session_id="session-001",
        action="resume",
    )

    assert result.state == "completed"
    assert result.command_id == "cmd-001"
