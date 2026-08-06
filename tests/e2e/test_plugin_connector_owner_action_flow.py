from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)
from hermes_agent_plugin.application.control_commands import CommandLedger
from hermes_agent_plugin.domain.control_lease import ControlLeaseManager
from hermes_agent_plugin.ports import local_relay as plugin_local_relay
from hermes_connector.adapters.platform.macos.plugin_control_relay import (
    MacOSPluginOwnerControlChannelFactory,
)
from hermes_connector.application.owner_control_lane import OwnerControlLane
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.owner_control import OwnerControlRequest

from tests.e2e.plugin_test_runtime import (
    LocalGatewayTestRuntime,
    create_connector_authority_provider,
    create_control_relay_test_resource,
    create_test_runtime_authority,
)

NOW = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
PROFILE = "default"
SESSION_KEY = "durable-owner-action-1"
CLIENT_INSTANCE_ID = UUID("33333333-3333-4333-8333-333333333333")
TRANSPORT_ID = UUID("22222222-2222-4222-8222-222222222222")


def _request(
    operation: str,
    body: Mapping[str, object],
    *,
    request_id: UUID | None = None,
) -> OwnerControlRequest:
    return OwnerControlRequest(
        request_id=request_id or uuid4(),
        control_transport_id=TRANSPORT_ID,
        operation=operation,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        body=body,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS UDS backend")
@pytest.mark.asyncio
async def test_owner_actions_are_idempotent_and_generation_fenced(
    tmp_path: Path,
) -> None:
    socket_root = Path("/tmp").resolve(strict=True) / (
        f"hctl-action-e2e-{os.getpid()}-{tmp_path.name[-8:]}"
    )
    paths = MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=socket_root / "local",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=socket_root / "control",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=socket_root / "observer",
    )
    backend = MacOSLocalRelayBackend(paths)
    previous_backend_factory = plugin_local_relay._backend_factory
    plugin_local_relay.configure_local_relay_backend(lambda: backend)

    owner_calls: list[tuple[str, str]] = []

    def owner_action(
        request: dict[str, object],
        _transport: object,
    ) -> Mapping[str, object]:
        params = request["params"]
        assert isinstance(params, dict)
        owner_calls.append(
            (
                str(request["method"]),
                str(params["client_request_id"]),
            )
        )
        return {"status": "accepted"}

    authority = create_test_runtime_authority(
        profile=PROFILE,
        runtime_generation="runtime-generation-7",
    )
    resource = create_control_relay_test_resource(
        authority=authority,
        owner_action=owner_action,
        leases=ControlLeaseManager(),
        commands=CommandLedger(),
    )
    runtime = LocalGatewayTestRuntime(
        resources=(resource,),
        runtime_authority=authority,
    )

    initial_provider = create_connector_authority_provider(authority)
    current_authority = [await initial_provider()]

    async def authority_provider() -> LocalRuntimeAuthority:
        return current_authority[0]

    lane = OwnerControlLane(
        factory=MacOSPluginOwnerControlChannelFactory(
            registry_directory=paths.control_registry_directory,
            socket_directory=paths.control_socket_directory,
            profile=PROFILE,
            provider="hermes-cloud",
            authority=authority_provider,
        ),
        utc_now=lambda: NOW,
    )

    try:
        runtime.install()
        runtime.start()

        opened = await lane.process(
            _request(
                "control.transport.open",
                {
                    "principal_id": "principal-1",
                    "client_instance_id": str(CLIENT_INSTANCE_ID),
                    "session_key": SESSION_KEY,
                    "profile": PROFILE,
                },
            )
        )
        assert opened.state == "succeeded"

        acquired = await lane.process(
            _request(
                "session.control.acquire",
                {"runtime_session_id": "runtime-session-7"},
            )
        )
        assert acquired.state == "succeeded"
        assert acquired.result is not None
        lease_id = acquired.result["lease_id"]
        assert isinstance(lease_id, str)

        interrupt_body = {
            "lease_id": lease_id,
            "client_request_id": "interrupt-request-1",
            "runtime_session_id": "runtime-session-7",
        }
        interrupt_request = _request("session.interrupt", interrupt_body)
        interrupted = await lane.process(interrupt_request)
        assert interrupted.state == "succeeded"
        assert interrupted.result == {
            "status": "accepted",
            "client_request_id": "interrupt-request-1",
        }
        assert owner_calls == [("session.interrupt", "interrupt-request-1")]

        connector_replay = await lane.process(interrupt_request)
        assert connector_replay == interrupted
        assert owner_calls == [("session.interrupt", "interrupt-request-1")]

        plugin_replay = await lane.process(
            _request("session.interrupt", interrupt_body)
        )
        assert plugin_replay.state == "succeeded"
        assert plugin_replay.result == interrupted.result
        assert owner_calls == [("session.interrupt", "interrupt-request-1")]

        command_status = await lane.process(
            _request(
                "session.command.status",
                {
                    "method": "session.interrupt",
                    "client_request_id": "interrupt-request-1",
                    "runtime_session_id": "runtime-session-7",
                },
            )
        )
        assert command_status.state == "succeeded"
        assert command_status.result == {
            "status": "accepted",
            "client_request_id": "interrupt-request-1",
        }

        prompt_body = {
            "lease_id": lease_id,
            "client_request_id": "prompt-request-1",
            "client_turn_id": "client-turn-1",
            "text": "Continue the current task.",
            "runtime_session_id": "runtime-session-7",
        }
        prompted = await lane.process(_request("prompt.submit", prompt_body))
        assert prompted.state == "succeeded"
        assert prompted.result == {
            "status": "accepted",
            "client_request_id": "prompt-request-1",
        }
        assert owner_calls == [
            ("session.interrupt", "interrupt-request-1"),
            ("prompt.submit", "prompt-request-1"),
        ]

        prompt_replay = await lane.process(_request("prompt.submit", prompt_body))
        assert prompt_replay.state == "succeeded"
        assert prompt_replay.result == prompted.result
        assert len(owner_calls) == 2

        prompt_conflict = await lane.process(
            _request(
                "prompt.submit",
                {
                    **prompt_body,
                    "text": "Use a conflicting payload.",
                },
            )
        )
        assert prompt_conflict.state == "failed"
        assert prompt_conflict.error == {
            "code": 4207,
            "reason": "request_id_payload_conflict",
        }
        assert len(owner_calls) == 2

        rotated_authority = create_test_runtime_authority(
            profile=PROFILE,
            runtime_generation="runtime-generation-8",
        )
        rotated_provider = create_connector_authority_provider(rotated_authority)
        current_authority[0] = await rotated_provider()

        stale_channel = await lane.process(_request("session.control.status", {}))
        assert stale_channel.state == "failed"
        assert stale_channel.error == {
            "code": 4214,
            "reason": "owner_adapter_unavailable",
        }
        assert len(owner_calls) == 2
    finally:
        try:
            await lane.close_all()
        finally:
            try:
                runtime.stop()
            finally:
                plugin_local_relay._backend_factory = previous_backend_factory
