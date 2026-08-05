from hermes_cloud.runtime_identity import RuntimeIdentityProjection


def test_runtime_identity_projection_keeps_runtime_separate_from_connector():
    projection = RuntimeIdentityProjection(
        connector_id="connector-1",
        runtime_id="runtime-1",
        runtime_generation="generation-1",
        profile="default",
        descriptor_hash="hash-1",
        extensions=("observer", "control"),
        capabilities=("session.observe", "session.control"),
    )

    assert projection.matches_generation("generation-1")
    assert projection.as_dict()["runtime_id"] == "runtime-1"
