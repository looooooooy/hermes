from hermes_cloud.runtime_identity.service import RuntimeIdentityService


class _Registry:
    def __init__(self):
        self.items = []

    def upsert(self, projection):
        self.items.append(projection)


def test_runtime_identity_service_accepts_valid_payload():
    registry = _Registry()
    service = RuntimeIdentityService(registry)

    result = service.verify_and_register(
        {
            "connector_id": "connector-1",
            "runtime_id": "runtime-1",
            "runtime_generation": "generation-1",
            "profile": "default",
            "descriptor_hash": "hash-1",
        }
    )

    assert result.accepted is True
    assert result.reason == "runtime_identity_verified"
    assert len(registry.items) == 1


def test_runtime_identity_service_rejects_missing_fields():
    service = RuntimeIdentityService(_Registry())

    result = service.verify_and_register({"runtime_id": "runtime-1"})

    assert result.accepted is False
    assert "missing_runtime_identity_fields" in result.reason
