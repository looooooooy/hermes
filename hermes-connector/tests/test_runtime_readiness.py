from hermes_connector.runtime.readiness import RuntimeReadiness


def test_runtime_readiness_type_exists():
    assert RuntimeReadiness is not None
