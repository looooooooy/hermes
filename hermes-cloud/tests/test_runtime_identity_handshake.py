from hermes_cloud.runtime_identity.handshake import RuntimeHandshake


def test_runtime_handshake_parses_runtime_identity():
    handshake = RuntimeHandshake.from_payload(
        {
            "connector_id": "connector-1",
            "runtime": {
                "runtime_id": "runtime-1",
                "runtime_generation": "generation-1",
                "profile": "default",
                "descriptor_hash": "hash-1",
            },
        }
    )

    assert handshake.runtime_id == "runtime-1"
    assert handshake.runtime_generation == "generation-1"
    assert handshake.profile == "default"
