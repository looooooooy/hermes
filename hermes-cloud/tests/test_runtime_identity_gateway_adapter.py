from hermes_cloud.runtime_identity.gateway_adapter import RuntimeIdentityGatewayAdapter


class _Result:
    accepted = True


class _Service:
    def verify_and_register(self, _handshake):
        return _Result()


def test_gateway_adapter_accepts_runtime_binding():
    adapter = RuntimeIdentityGatewayAdapter(_Service())

    result = adapter.handle_connector_hello(
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

    assert result.status == "active"
    assert result.runtime_id == "runtime-1"
