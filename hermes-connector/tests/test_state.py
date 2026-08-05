from __future__ import annotations

import unittest

from hermes_connector.domain.state import (
    CONNECTOR_TRANSITIONS,
    ConnectorState,
    InvalidStateTransition,
    transition_connector,
)

EXPECTED_TRANSITIONS = {
    ConnectorState.INSTALLED: {
        ConnectorState.WAITING_FOR_AGENT,
        ConnectorState.UNPAIRED,
    },
    ConnectorState.WAITING_FOR_AGENT: {ConnectorState.UNPAIRED},
    ConnectorState.UNPAIRED: {ConnectorState.PAIRING},
    ConnectorState.PAIRING: {
        ConnectorState.CLOUD_CONNECTING,
        ConnectorState.UNPAIRED,
    },
    ConnectorState.CLOUD_CONNECTING: {ConnectorState.AGENT_DISCOVERING},
    ConnectorState.AGENT_DISCOVERING: {
        ConnectorState.RECONCILING,
        ConnectorState.AGENT_UNAVAILABLE,
    },
    ConnectorState.RECONCILING: {ConnectorState.READY},
    ConnectorState.READY: {
        ConnectorState.DRAINING,
        ConnectorState.DEGRADED,
        ConnectorState.AGENT_UNAVAILABLE,
        ConnectorState.REVOKED,
    },
    ConnectorState.DRAINING: {ConnectorState.STOPPED},
    ConnectorState.DEGRADED: {ConnectorState.RECONCILING},
    ConnectorState.AGENT_UNAVAILABLE: {ConnectorState.RECONCILING},
    ConnectorState.STOPPED: set(),
    ConnectorState.REVOKED: {ConnectorState.UNPAIRED},
}


class ConnectorStateTest(unittest.TestCase):
    def test_every_allowed_transition_is_accepted(self) -> None:
        for source, targets in EXPECTED_TRANSITIONS.items():
            for target in targets:
                with self.subTest(source=source.value, target=target.value):
                    self.assertIs(transition_connector(source, target), target)

    def test_every_unlisted_transition_is_rejected(self) -> None:
        for source in ConnectorState:
            for target in ConnectorState:
                if target in EXPECTED_TRANSITIONS[source]:
                    continue
                with (
                    self.subTest(
                        source=source.value,
                        target=target.value,
                    ),
                    self.assertRaises(InvalidStateTransition),
                ):
                    transition_connector(source, target)

    def test_transition_rules_are_complete_and_immutable(self) -> None:
        self.assertEqual(set(CONNECTOR_TRANSITIONS), set(ConnectorState))
        self.assertEqual(
            {source: set(targets) for source, targets in CONNECTOR_TRANSITIONS.items()},
            EXPECTED_TRANSITIONS,
        )

        with self.assertRaises(TypeError):
            CONNECTOR_TRANSITIONS[ConnectorState.INSTALLED] = frozenset()  # type: ignore[index]
        with self.assertRaises(AttributeError):
            CONNECTOR_TRANSITIONS[ConnectorState.READY].add(  # type: ignore[attr-defined]
                ConnectorState.STOPPED
            )


if __name__ == "__main__":
    unittest.main()
