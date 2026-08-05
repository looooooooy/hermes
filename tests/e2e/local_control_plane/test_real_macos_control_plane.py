"""Plugin ↔ Connector closure over the production macOS UDS adapters."""

from __future__ import annotations

import asyncio
import sys

import pytest

from .contract_authority import LocalGatewayContractAuthority
from .harness import (
    exercise_active_session,
    exercise_incompatible_contract,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the verified Local Gateway adapter in this slice is macOS-only",
)


def test_plugin_and_connector_reach_active_over_real_macos_uds() -> None:
    authority = LocalGatewayContractAuthority.load()

    evidence = asyncio.run(exercise_active_session(authority))

    assert authority.version == 1
    assert evidence.plugin_ready is True
    assert evidence.descriptor_trusted is True
    assert evidence.endpoint_count == 1
    assert evidence.connector_state == "active"
    assert evidence.state_history[:4] == (
        "disconnected",
        "connecting",
        "negotiating",
        "active",
    )
    assert evidence.runtime_generation == authority.welcome["runtime_generation"]
    assert evidence.accepted_capabilities == tuple(
        authority.welcome["accepted_capabilities"]
    )
    assert evidence.unavailable_optional_capabilities == tuple(
        authority.welcome["unavailable_optional_capabilities"]
    )
    assert evidence.descriptor_removed is True
    assert evidence.socket_removed is True
    assert evidence.leaked_async_tasks == ()
    assert evidence.leaked_threads == ()


def test_incompatible_contract_fails_closed_without_command_effect() -> None:
    authority = LocalGatewayContractAuthority.load()

    evidence = asyncio.run(exercise_incompatible_contract(authority))

    assert evidence.error_code == 4300
    assert evidence.error_reason == "contract_unsupported"
    assert evidence.plugin_still_ready is True
    assert evidence.command_effects == 0
    assert evidence.descriptor_removed is True
    assert evidence.socket_removed is True
    assert evidence.leaked_async_tasks == ()
    assert evidence.leaked_threads == ()
