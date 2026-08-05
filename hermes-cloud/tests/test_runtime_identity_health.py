from hermes_cloud.runtime_identity.health import (
    RuntimeHealthProjection,
    RuntimeHealthState,
)


def test_runtime_health_projection_serializes_state() -> None:
    projection = RuntimeHealthProjection(
        runtime_id="runtime-1",
        runtime_generation="generation-1",
        state=RuntimeHealthState.ACTIVE,
    )

    payload = projection.as_dict()

    assert payload["runtime_id"] == "runtime-1"
    assert payload["runtime_generation"] == "generation-1"
    assert payload["state"] == "active"
