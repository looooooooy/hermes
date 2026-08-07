from hermes_connector.runtime.handshake import RuntimeHandshakePayload


class FakeDescriptor:
    runtime_id = "runtime-1"
    runtime_generation = "generation-1"
    profile = "default"

    def fingerprint(self):
        return "sha-runtime-1"


def test_runtime_handshake_payload_from_descriptor():
    payload = RuntimeHandshakePayload.from_descriptor(FakeDescriptor())

    assert payload.as_dict() == {
        "runtime_id": "runtime-1",
        "runtime_generation": "generation-1",
        "profile": "default",
        "descriptor_hash": "sha-runtime-1",
    }
