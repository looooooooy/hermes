from hermes_runtime.control.session_action_router import (
    SessionActionRequest,
    SessionActionRouter,
)


class FakeSession:
    def __init__(self):
        self.actions = []

    def interrupt(self):
        self.actions.append("interrupt")

    def resume(self):
        self.actions.append("resume")

    def approve(self, payload):
        self.actions.append(("approve", payload))


def test_dispatch_resume():
    session = FakeSession()

    result = SessionActionRouter().dispatch(
        SessionActionRequest(
            command_id="cmd-1",
            action="resume",
            runtime_generation="g1",
            session_id="s1",
            payload={},
        ),
        session,
    )

    assert result.state == "completed"
    assert session.actions == ["resume"]
