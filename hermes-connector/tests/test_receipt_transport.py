from hermes_connector.control_plane.receipt_transport import (
    CommandReceiptTransport,
    ReceiptEnvelope,
)


class Publisher:
    def __init__(self):
        self.payload = None

    def publish(self, payload):
        self.payload = payload


def test_receipt_transport_publishes_runtime_result():
    publisher = Publisher()
    transport = CommandReceiptTransport(publisher)

    result = transport.send(
        ReceiptEnvelope(
            command_id="cmd-1",
            runtime_generation="rt-1",
            session_id="session-1",
            state="completed",
        )
    )

    assert result["state"] == "completed"
    assert publisher.payload == result
