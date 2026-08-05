from __future__ import annotations

import inspect

import pytest

from hermes_cloud.domain.persistence import (
    ALLOWED_PAIRING_SESSION_TRANSITIONS,
    InvalidPairingSessionTransition,
    PairingSessionState,
    require_pairing_session_transition,
)


def test_pairing_session_state_graph_and_allowed_transition_table() -> None:
    assert ALLOWED_PAIRING_SESSION_TRANSITIONS == {
        PairingSessionState.PENDING: frozenset(
            {
                PairingSessionState.CLAIMED,
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
        ),
        PairingSessionState.CLAIMED: frozenset(
            {
                PairingSessionState.CONFIRMED,
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
        ),
        PairingSessionState.CONFIRMED: frozenset(
            {
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
        ),
        PairingSessionState.EXPIRED: frozenset(),
        PairingSessionState.CANCELLED: frozenset(),
    }
    require_pairing_session_transition(
        PairingSessionState.PENDING,
        PairingSessionState.CLAIMED,
    )

    with pytest.raises(InvalidPairingSessionTransition):
        require_pairing_session_transition(
            PairingSessionState.CONFIRMED,
            PairingSessionState.PENDING,
        )

    documentation = inspect.getdoc(require_pairing_session_transition)
    assert documentation is not None
    assert "pending" in documentation
    assert "-->" in documentation
    assert "Allowed transitions" in documentation
