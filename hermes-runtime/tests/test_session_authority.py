from hermes_runtime.control.session_authority import (
    SessionAuthority,
    SessionBinding,
)


def test_session_binding_requires_matching_generation():
    authority = SessionAuthority()
    authority.bind(
        SessionBinding(
            session_id="session-1",
            runtime_generation="runtime-1",
            profile="default",
        )
    )

    assert authority.resolve("session-1", "runtime-1").profile == "default"

    try:
        authority.resolve("session-1", "runtime-2")
    except ValueError:
        pass
    else:
        raise AssertionError("runtime mismatch must fail")
