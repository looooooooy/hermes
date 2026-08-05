from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from hermes_connector.adapters.platform.macos import plugin_control_relay
from hermes_connector.adapters.platform.macos.plugin_control_relay import (
    MacOSPluginOwnerControlChannelFactory,
)
from hermes_connector.application.owner_control_lane import OwnerControlLane
from hermes_connector.domain.owner_control import OwnerControlRequest
from tests.e2e.plugin_test_runtime import (
    LocalGatewayTestRuntime,
    create_connector_authority_provider,
    create_control_relay_test_resource,
    create_test_runtime_authority,
)

from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)
from hermes_agent_plugin.application.control_commands import CommandLedger
from hermes_agent_plugin.domain.control_lease import (
    ControlBinding,
    ControlLease,
    ControlLeaseManager,
)
from hermes_agent_plugin.ports import local_relay as plugin_local_relay

NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
PROFILE = "default"
SESSION_KEY = "durable-root-1"
CLIENT_INSTANCE_ID = UUID("33333333-3333-4333-8333-333333333333")
FIRST_TRANSPORT_ID = UUID("22222222-2222-4222-8222-222222222222")
SECOND_TRANSPORT_ID = UUID("99999999-9999-4999-8999-999999999999")
FAULTED_RENEW_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _CountingLeaseManager(ControlLeaseManager):
    def __init__(self) -> None:
        leases = iter(("lease-secret-1", "lease-secret-2"))
        super().__init__(
            ttl_seconds=30,
            reconnect_grace_seconds=5,
            lease_id_factory=lambda: next(leases),
        )
        self.acquire_calls = 0
        self.renew_calls = 0

    def acquire(self, binding: ControlBinding) -> ControlLease:
        self.acquire_calls += 1
        return super().acquire(binding)

    def renew(self, binding: ControlBinding, *, lease_id: str) -> ControlLease:
        self.renew_calls += 1
        return super().renew(binding, lease_id=lease_id)


class _FaultAfterResponse:
    """Consume one real response, then model loss after the Plugin effect."""

    def __init__(self, websocket: object) -> None:
        self._websocket = websocket
        self._request_id: str | None = None
        self._faulted = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._websocket, name)

    async def send(self, message: object) -> None:
        if isinstance(message, (str, bytes)):
            payload = json.loads(message)
            if payload.get("id") == str(FAULTED_RENEW_ID):
                self._request_id = str(FAULTED_RENEW_ID)
        await self._websocket.send(message)

    async def recv(self, *args: object, **kwargs: object) -> object:
        response = await self._websocket.recv(*args, **kwargs)
        if self._request_id is not None and not self._faulted:
            self._faulted = True
            raise ConnectionError("injected response loss")
        return response

    async def close(self, *args: object, **kwargs: object) -> object:
        return await self._websocket.close(*args, **kwargs)


def _request(
    transport_id: UUID,
    operation: str,
    body: Mapping[str, object],
    *,
    request_id: UUID | None = None,
) -> OwnerControlRequest:
    return OwnerControlRequest(
        request_id=request_id or uuid4(),
        control_transport_id=transport_id,
        operation=operation,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
        body=body,
    )


def _open_request(transport_id: UUID) -> OwnerControlRequest:
    return _request(
        transport_id,
        "control.transport.open",
        {
            "principal_id": "principal-1",
            "client_instance_id": str(CLIENT_INSTANCE_ID),
            "session_key": SESSION_KEY,
            "profile": PROFILE,
        },
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS UDS backend")
@pytest.mark.asyncio
async def test_real_plugin_connector_owner_control_lifecycle_and_unknown_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_root = Path("/tmp").resolve(strict=True) / (
        f"hctl-owner-e2e-{os.getpid()}-{tmp_path.name[-8:]}"
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
    leases = _CountingLeaseManager()
    owner_calls: list[str] = []

    def owner_action(
        request: dict[str, object],
        _transport: object,
    ) -> Mapping[str, object]:
        owner_calls.append(str(request["method"]))
        return {"status": "accepted"}

    authority = create_test_runtime_authority(
        profile=PROFILE,
        runtime_generation="runtime-generation-7",
    )
    resource = create_control_relay_test_resource(
        authority=authority,
        owner_action=owner_action,
        leases=leases,
        commands=CommandLedger(),
    )
    runtime = LocalGatewayTestRuntime(
        resources=(resource,),
        runtime_authority=authority,
    )

    real_unix_connect = plugin_control_relay.unix_connect

    async def faulting_unix_connect(*args: object, **kwargs: object) -> object:
        websocket = await real_unix_connect(*args, **kwargs)
        return _FaultAfterResponse(websocket)

    monkeypatch.setattr(
        plugin_control_relay,
        "unix_connect",
        faulting_unix_connect,
    )
    lane = OwnerControlLane(
        factory=MacOSPluginOwnerControlChannelFactory(
            registry_directory=paths.control_registry_directory,
            socket_directory=paths.control_socket_directory,
            profile=PROFILE,
            provider="hermes-cloud",
            authority=create_connector_authority_provider(authority),
        ),
        utc_now=lambda: NOW,
    )

    try:
        runtime.install()
        runtime.start()
        first_open = await lane.process(_open_request(FIRST_TRANSPORT_ID))
        second_open = await lane.process(_open_request(SECOND_TRANSPORT_ID))
        assert first_open.state == second_open.state == "succeeded"

        first_acquire = _request(
            FIRST_TRANSPORT_ID,
            "session.control.acquire",
            {
                "runtime_session_id": "runtime-session-7",
                "runtime_generation": "runtime-generation-7",
            },
        )
        acquired = await lane.process(first_acquire)
        assert acquired.state == "succeeded"
        assert acquired.result is not None
        first_lease = acquired.result["lease_id"]
        acquire_calls = leases.acquire_calls

        replay = await lane.process(first_acquire)
        assert replay == acquired
        assert leases.acquire_calls == acquire_calls

        conflict = await lane.process(
            _request(
                SECOND_TRANSPORT_ID,
                "session.control.acquire",
                {
                    "runtime_session_id": "runtime-session-7",
                    "runtime_generation": "runtime-generation-7",
                },
            )
        )
        assert conflict.state == "failed"
        assert conflict.error == {
            "code": 4203,
            "reason": "controller_conflict",
        }

        status = await lane.process(
            _request(
                SECOND_TRANSPORT_ID,
                "session.control.status",
                {},
            )
        )
        assert status.state == "succeeded"
        assert status.result is not None
        assert status.result["controller_kind"] == "mobile"
        assert "lease_id" not in status.result

        closed = await lane.process(
            _request(
                FIRST_TRANSPORT_ID,
                "control.transport.close",
                {"reason": "client_disconnected"},
            )
        )
        assert closed.state == "succeeded"

        rebound = None
        for _attempt in range(100):
            candidate = await lane.process(
                _request(
                    SECOND_TRANSPORT_ID,
                    "session.control.acquire",
                    {
                        "runtime_session_id": "runtime-session-7",
                        "runtime_generation": "runtime-generation-7",
                    },
                )
            )
            if candidate.state == "succeeded":
                rebound = candidate
                break
            assert candidate.error == {
                "code": 4203,
                "reason": "controller_conflict",
            }
            await asyncio.sleep(0.01)

        assert rebound is not None
        assert rebound.result is not None
        second_lease = rebound.result["lease_id"]
        assert second_lease == "lease-secret-2"
        assert second_lease != first_lease

        stale = await lane.process(
            _request(
                SECOND_TRANSPORT_ID,
                "session.control.renew",
                {"lease_id": first_lease},
            )
        )
        assert stale.state == "failed"
        assert stale.error == {"code": 4206, "reason": "lease_mismatch"}

        renew_calls = leases.renew_calls
        revision_before = leases.revision(
            session_key=SESSION_KEY,
            profile=PROFILE,
        )
        faulted_renew = _request(
            SECOND_TRANSPORT_ID,
            "session.control.renew",
            {"lease_id": second_lease},
            request_id=FAULTED_RENEW_ID,
        )
        unknown = await lane.process(faulted_renew)
        assert unknown.state == "unknown"
        assert unknown.error == {"code": 4307, "reason": "effect_unknown"}
        assert leases.renew_calls == renew_calls + 1
        assert (
            leases.revision(session_key=SESSION_KEY, profile=PROFILE)
            == revision_before + 1
        )

        replayed_unknown = await lane.process(faulted_renew)
        assert replayed_unknown == unknown
        assert leases.renew_calls == renew_calls + 1
        assert owner_calls == []
        assert not tuple(tmp_path.rglob("*.sqlite*"))
    finally:
        try:
            await lane.close_all()
        finally:
            try:
                runtime.stop()
            finally:
                plugin_local_relay._backend_factory = previous_backend_factory
