"""Canonical lifecycle domain tests."""

from pathlib import Path


def test_canonical_lifecycle_domain_owns_the_transition_model() -> None:
    module_path = (
        Path(__file__).parents[3] / "src/hermes_agent_plugin/domain/lifecycle.py"
    )
    assert module_path.is_file(), "canonical lifecycle domain is missing"

    from hermes_agent_plugin.domain.lifecycle import (
        ALLOWED_LIFECYCLE_TRANSITIONS,
        GatewayState,
    )

    assert ALLOWED_LIFECYCLE_TRANSITIONS[GatewayState.READY] == frozenset(
        {GatewayState.DRAINING}
    )
