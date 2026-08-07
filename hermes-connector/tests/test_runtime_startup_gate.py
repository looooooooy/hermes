from hermes_connector.runtime.startup import RuntimeStartupGate


class FakeVerifier:
    def verify(self, _descriptor):
        return type("Result", (), {"verified": True})()


def test_runtime_startup_requires_verified_binding():
    gate = RuntimeStartupGate(FakeVerifier())
    assert gate._verifier is not None
