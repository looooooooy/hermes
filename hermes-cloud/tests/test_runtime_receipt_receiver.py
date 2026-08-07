from hermes_cloud.runtime_identity.receipt_receiver import RuntimeReceiptReceiver


class FakeStatusStore:
    def __init__(self):
        self.value = None

    def update(self, **kwargs):
        self.value = kwargs


def test_receipt_receiver_updates_command_status():
    store = FakeStatusStore()
    receiver = RuntimeReceiptReceiver(store)

    result = receiver.receive(
        {
            "command_id": "cmd-1",
            "runtime_generation": "runtime-1",
            "state": "completed",
            "detail": "ok",
        }
    )

    assert result.state == "completed"
    assert store.value["command_id"] == "cmd-1"
