from hermes_cloud.runtime_identity.command_status_store import (
    RuntimeCommandStatusStore,
)


def test_command_status_update():
    store = RuntimeCommandStatusStore()

    status = store.update(
        "cmd-1",
        "runtime-1",
        "completed",
        "ok",
    )

    assert status.state == "completed"
    assert store.get("cmd-1").detail == "ok"
