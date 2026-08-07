import pytest

from hermes_release_publisher import PublisherError, ReleaseState, ReleaseStateMachine

NOW = "2026-08-07T14:00:00Z"


def advance(record, state, generation):
    return ReleaseStateMachine.transition(
        record,
        next_state=state,
        transition_generation=generation,
        reason_code="qualification",
        occurred_at=NOW,
    )


def test_happy_release_train_reaches_stable_only_in_order() -> None:
    record = ReleaseStateMachine.initial(
        release_id="1.4.2+20260807.3.g9839a049", occurred_at=NOW
    )
    for generation, state in enumerate(
        [
            ReleaseState.BUILT,
            ReleaseState.QUALIFIED,
            ReleaseState.SIGNED,
            ReleaseState.UPLOADED,
            ReleaseState.VERIFIED,
            ReleaseState.CANARY,
            ReleaseState.BETA,
            ReleaseState.STABLE,
        ],
        start=2,
    ):
        record = advance(record, state, generation)

    assert record.state is ReleaseState.STABLE
    assert record.transition_generation == 9


def test_release_cannot_skip_qualification() -> None:
    record = ReleaseStateMachine.initial(
        release_id="1.4.2+20260807.3.g9839a049", occurred_at=NOW
    )
    record = advance(record, ReleaseState.BUILT, 2)

    with pytest.raises(PublisherError, match="illegal release state transition"):
        advance(record, ReleaseState.SIGNED, 3)


def test_transition_generation_must_be_monotonic() -> None:
    record = ReleaseStateMachine.initial(
        release_id="1.4.2+20260807.3.g9839a049", occurred_at=NOW
    )

    with pytest.raises(PublisherError, match="must increase monotonically"):
        advance(record, ReleaseState.BUILT, 1)


def test_stable_release_can_be_blocked_but_not_unblocked_in_place() -> None:
    record = ReleaseStateMachine.initial(
        release_id="1.4.2+20260807.3.g9839a049", occurred_at=NOW
    )
    for generation, state in enumerate(
        [
            ReleaseState.BUILT,
            ReleaseState.QUALIFIED,
            ReleaseState.SIGNED,
            ReleaseState.UPLOADED,
            ReleaseState.VERIFIED,
            ReleaseState.CANARY,
            ReleaseState.BETA,
            ReleaseState.STABLE,
        ],
        start=2,
    ):
        record = advance(record, state, generation)

    blocked = ReleaseStateMachine.transition(
        record,
        next_state=ReleaseState.BLOCKED,
        transition_generation=10,
        reason_code="security-block",
        occurred_at=NOW,
        evidence_sha256="a" * 64,
    )
    assert blocked.state is ReleaseState.BLOCKED

    with pytest.raises(PublisherError, match="illegal release state transition"):
        advance(blocked, ReleaseState.STABLE, 11)


def test_blocked_release_can_only_escalate_to_revoked() -> None:
    record = ReleaseStateMachine.initial(
        release_id="1.4.2+20260807.3.g9839a049", occurred_at=NOW
    )
    record = advance(record, ReleaseState.BUILT, 2)
    record = advance(record, ReleaseState.QUALIFIED, 3)
    blocked = ReleaseStateMachine.transition(
        record,
        next_state=ReleaseState.BLOCKED,
        transition_generation=4,
        reason_code="supply-chain-hold",
        occurred_at=NOW,
    )
    revoked = ReleaseStateMachine.transition(
        blocked,
        next_state=ReleaseState.REVOKED,
        transition_generation=5,
        reason_code="key-compromise",
        occurred_at=NOW,
    )
    assert revoked.state is ReleaseState.REVOKED
