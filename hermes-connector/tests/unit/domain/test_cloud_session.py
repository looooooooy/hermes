from __future__ import annotations

import pytest

from hermes_connector.domain.cloud_session import (
    CLOUD_SESSION_TRANSITIONS,
    CloudSessionState,
    InvalidCloudSessionTransition,
    transition_cloud_session,
)


def test_cloud_session_transition_table_matches_connector_protocol_v1() -> None:
    assert CLOUD_SESSION_TRANSITIONS == {
        CloudSessionState.DISCONNECTED: frozenset({CloudSessionState.CONNECTING}),
        CloudSessionState.CONNECTING: frozenset(
            {
                CloudSessionState.NEGOTIATING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.NEGOTIATING: frozenset(
            {
                CloudSessionState.ACTIVE,
                CloudSessionState.RECONCILING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.ACTIVE: frozenset(
            {
                CloudSessionState.RECONCILING,
                CloudSessionState.DRAINING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.RECONCILING: frozenset(
            {
                CloudSessionState.ACTIVE,
                CloudSessionState.DRAINING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.DRAINING: frozenset({CloudSessionState.DISCONNECTED}),
    }


def test_cloud_session_rejects_undocumented_transition() -> None:
    with pytest.raises(InvalidCloudSessionTransition):
        transition_cloud_session(
            CloudSessionState.DISCONNECTED,
            CloudSessionState.ACTIVE,
        )
