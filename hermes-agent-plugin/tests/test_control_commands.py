from __future__ import annotations

import threading

import pytest

from hermes_agent_plugin.application.control_commands import (
    CommandIdentity,
    CommandLedger,
    RequestPayloadConflict,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def identity(*, client_instance_id: str = "client-1") -> CommandIdentity:
    return CommandIdentity(
        session_key="durable-root-1",
        user_id="user-1",
        provider="basic",
        client_instance_id=client_instance_id,
    )


def test_same_request_and_payload_returns_prior_result_without_running_twice() -> None:
    calls: list[str] = []
    ledger = CommandLedger()

    first = ledger.execute(
        identity(),
        method="prompt.submit",
        client_request_id="request-1",
        payload={"text": "hello", "client_turn_id": "turn-1"},
        operation=lambda: (
            calls.append("ran") or {"status": "accepted", "server_turn_id": "server-1"}
        ),
    )
    replay = ledger.execute(
        identity(),
        method="prompt.submit",
        client_request_id="request-1",
        payload={"client_turn_id": "turn-1", "text": "hello"},
        operation=lambda: calls.append("must-not-run") or {"status": "accepted"},
    )

    assert calls == ["ran"]
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.result == first.result
    assert (
        ledger.replay(
            identity(),
            method="prompt.submit",
            client_request_id="request-1",
            payload={"client_turn_id": "turn-1", "text": "hello"},
        ).result
        == first.result
    )
    assert ledger.status(
        identity(),
        method="prompt.submit",
        client_request_id="request-1",
    ).result == {
        "status": "accepted",
        "client_request_id": "request-1",
        "server_turn_id": "server-1",
    }


def test_same_request_id_with_different_payload_conflicts_without_exposing_payload() -> (
    None
):
    ledger = CommandLedger()
    ledger.execute(
        identity(),
        method="prompt.submit",
        client_request_id="request-1",
        payload={"text": "first-secret-prompt"},
        operation=lambda: {"status": "accepted"},
    )

    with pytest.raises(RequestPayloadConflict) as conflict:
        ledger.execute(
            identity(),
            method="prompt.submit",
            client_request_id="request-1",
            payload={"text": "second-secret-prompt"},
            operation=lambda: {"status": "accepted"},
        )

    assert "first-secret-prompt" not in str(conflict.value)
    assert "second-secret-prompt" not in str(conflict.value)


def test_status_is_scoped_to_authenticated_client_and_unknown_never_runs_an_operation() -> (
    None
):
    ledger = CommandLedger()
    ledger.execute(
        identity(),
        method="session.interrupt",
        client_request_id="request-1",
        payload={},
        operation=lambda: {"status": "accepted"},
    )

    assert (
        ledger.status(
            identity(client_instance_id="client-other"),
            method="session.interrupt",
            client_request_id="request-1",
        )
        is None
    )
    assert ledger.status(
        identity(),
        method="session.interrupt",
        client_request_id="missing",
    ) is None


def test_status_is_keyed_by_method_and_returns_only_the_generic_projection() -> None:
    ledger = CommandLedger()
    for method, result in (
        (
            "approval.respond",
            {
                "status": "accepted",
                "kind": "approval",
                "request_id": "approval-secret",
                "client_request_id": "request-shared",
                "control_revision": 9,
            },
        ),
        (
            "clarify.respond",
            {
                "status": "queued",
                "kind": "clarify",
                "request_id": "clarify-secret",
                "client_request_id": "request-shared",
                "control_revision": 10,
            },
        ),
    ):
        ledger.execute(
            identity(),
            method=method,
            client_request_id="request-shared",
            payload={"method": method},
            operation=lambda result=result: result,
        )

    approval = ledger.status(
        identity(),
        method="approval.respond",
        client_request_id="request-shared",
    )
    clarify = ledger.status(
        identity(),
        method="clarify.respond",
        client_request_id="request-shared",
    )

    assert approval is not None
    assert approval.result == {
        "status": "accepted",
        "client_request_id": "request-shared",
    }
    assert clarify is not None
    assert clarify.result == {
        "status": "queued",
        "client_request_id": "request-shared",
    }


def test_unresolved_owner_effect_has_no_successful_status_projection() -> None:
    calls = 0
    ledger = CommandLedger()

    def unresolved() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("owner outcome unavailable")

    ledger.execute(
        identity(),
        method="session.interrupt",
        client_request_id="request-unknown",
        payload={},
        operation=unresolved,
    )

    assert ledger.status(
        identity(),
        method="session.interrupt",
        client_request_id="request-unknown",
    ) is None
    assert ledger.status(
        identity(),
        method="session.interrupt",
        client_request_id="request-unknown",
    ) is None
    assert calls == 1


def test_ttl_and_lru_bounds_drop_old_results() -> None:
    clock = FakeClock()
    ledger = CommandLedger(ttl_seconds=10, max_entries=2, monotonic=clock)
    for number in range(3):
        ledger.execute(
            identity(),
            method="prompt.submit",
            client_request_id=f"request-{number}",
            payload={"text": str(number)},
            operation=lambda number=number: {"status": "accepted", "number": number},
        )

    assert ledger.status(
        identity(), method="prompt.submit", client_request_id="request-0"
    ) is None
    assert ledger.status(
        identity(), method="prompt.submit", client_request_id="request-2"
    ) is not None

    clock.now += 11
    assert ledger.status(
        identity(), method="prompt.submit", client_request_id="request-2"
    ) is None


def test_concurrent_duplicate_executes_owner_action_once() -> None:
    ledger = CommandLedger()
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    results = []

    def operation() -> dict:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return {"status": "queued"}

    def invoke() -> None:
        results.append(
            ledger.execute(
                identity(),
                method="prompt.submit",
                client_request_id="request-1",
                payload={"text": "hello"},
                operation=operation,
            )
        )

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert calls == 1
    assert sorted(result.replayed for result in results) == [False, True]
    assert all(result.result == {"status": "queued"} for result in results)
