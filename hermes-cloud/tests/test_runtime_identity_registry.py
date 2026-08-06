from hermes_cloud.runtime_identity.projection import RuntimeIdentityProjection
from hermes_cloud.runtime_identity.registry import RuntimeIdentityRegistry


def test_runtime_identity_registry_upserts_runtime():
    registry = RuntimeIdentityRegistry()
    projection = RuntimeIdentityProjection(
        connector_id="connector-1",
        runtime_id="runtime-1",
        runtime_generation="generation-1",
        profile="default",
        descriptor_hash="hash-1",
        extensions=("observer", "control"),
        capabilities=("session.observe",),
    )

    result = registry.upsert(projection)

    assert result.created is True
    assert registry.get("runtime-1") == projection
