import pytest

from hermes_runtime.control.session_authority import SessionAuthority, SessionBinding


class FakeSession:
    def interrupt(self):
        pass

    def resume(self):
        pass

    def approve(self, payload):
        pass


def test_session_binding_requires_matching_generation():
    authority = SessionAuthority()
    controller = FakeSession()
    authority.bind(
        SessionBinding(
            session_id="session-1",
            runtime_generation="runtime-1",
            profile="default",
            controller=controller,
        )
    )

    binding = authority.resolve("session-1", "runtime-1")
    assert binding.profile == "default"
    assert binding.controller is controller

    with pytest.raises(ValueError, match="runtime generation mismatch"):
        authority.resolve("session-1", "runtime-2")


def test_session_binding_rejects_same_generation_controller_conflict():
    authority = SessionAuthority()
    authority.bind(
        SessionBinding("session-1", "runtime-1", "default", FakeSession())
    )

    with pytest.raises(ValueError, match="session binding conflict"):
        authority.bind(
            SessionBinding("session-1", "runtime-1", "default", FakeSession())
        )
