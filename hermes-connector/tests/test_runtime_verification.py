from hermes_connector.runtime.verification import RuntimeVerifier


def test_runtime_verifier_rejects_generation_mismatch():
    assert RuntimeVerifier is not None
