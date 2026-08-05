from __future__ import annotations

import json
import os
import threading
import time
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

from hermes_agent_plugin.adapters.local_protocol.control_relay import (
    list_control_endpoints,
    start_control_endpoint,
)
from hermes_agent_plugin.adapters.platform.macos import control_relay
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)
from hermes_agent_plugin.application.control_commands import CommandLedger
from hermes_agent_plugin.application.control_dispatcher import (
    ControlRequestDispatcher,
)
from hermes_agent_plugin.domain.control_lease import ControlLeaseManager
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2


def test_production_control_dispatcher_is_packaged() -> None:
    module_name = "hermes_agent_plugin.application.control_dispatcher"

    assert find_spec(module_name) is not None
    module = import_module(module_name)
    assert module.ControlRequestDispatcher is not None


def _claims() -> dict[str, str]:
    return {
        "user_id": "user-1",
        "provider": "basic",
        "connection_role": "control",
        "client_instance_id": "11111111-1111-4111-8111-111111111111",
        "session_key": "durable-root-1",
        "profile": "default",
    }


class _ControlTransport:
    connection_role = "control"
    transport_id = "transport-race"
    auth_claims = _claims()


def test_rollover_linearizes_with_validation_before_lease_mint() -> None:
    mint_calls = 0

    def mint_lease() -> str:
        nonlocal mint_calls
        mint_calls += 1
        return "first-mint-belongs-to-g2"

    leases = ControlLeaseManager(lease_id_factory=mint_lease)
    leases.bind_runtime(
        profile="default",
        runtime_generation="runtime-generation-1",
        ready=True,
    )
    validation_started = threading.Event()
    resume_validation = threading.Event()

    def paused_validator(_binding) -> None:
        validation_started.set()
        assert resume_validation.wait(timeout=1)

    dispatcher = ControlRequestDispatcher(
        owner_action=lambda _request, _transport: {"status": "accepted"},
        binding_validator=paused_validator,
        leases=leases,
    )
    request = {
        "jsonrpc": "2.0",
        "id": "stale-acquire",
        "method": "session.control.acquire",
        "params": {
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
    }
    responses: list[dict] = []
    worker = threading.Thread(
        target=lambda: responses.append(
            dispatcher.dispatch(request, _ControlTransport())
        )
    )
    worker.start()
    assert validation_started.wait(timeout=1)

    leases.bind_runtime(
        profile="default",
        runtime_generation="runtime-generation-2",
        ready=True,
    )
    resume_validation.set()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert responses[0]["error"]["message"] == "session_binding_mismatch"
    current = leases.acquire(
        dispatcher._binding(
            _ControlTransport(),
            {
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-2",
            },
        )
    )
    assert current.lease_id == "first-mint-belongs-to-g2"
    assert mint_calls == 1


def _rpc(
    websocket: object,
    *,
    request_id: str,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    websocket.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
    )
    return json.loads(websocket.recv())


def _attach(websocket: object, request_id: str) -> None:
    response = _rpc(
        websocket,
        request_id=request_id,
        method="relay.control.attach",
        params={"claims": _claims()},
    )
    assert response["result"] == {
        "attached": True,
        "connection_role": "control",
    }


def test_real_uds_dispatcher_binds_lease_and_ledger_to_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = tmp_path / "registry"
    temporary_directory = Path("/tmp").resolve(strict=True)
    socket_dir = temporary_directory / f"hctl-dispatch-{os.getpid()}"
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(socket_dir))
    paths = MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=temporary_directory
        / f"hlocal-dispatch-{os.getpid()}",
        control_registry_directory=registry,
        control_socket_directory=socket_dir,
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=temporary_directory
        / f"hobserver-dispatch-{os.getpid()}",
    )
    backend = MacOSLocalRelayBackend(paths)
    issued_leases = iter(("lease-secret-1", "lease-secret-2"))
    owner_calls: list[str] = []
    baseline_workers = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("control-owner-action-")
    }

    def owner_action(request: dict, _transport: object) -> dict[str, object]:
        owner_calls.append(request["method"])
        params = request["params"]
        return {
            "status": "accepted",
            "client_request_id": params["client_request_id"],
            "client_turn_id": params["client_turn_id"],
            "server_turn_id": "server-turn-1",
        }

    dispatcher = ControlRequestDispatcher(
        leases=ControlLeaseManager(
            ttl_seconds=30,
            reconnect_grace_seconds=5,
            lease_id_factory=lambda: next(issued_leases),
        ),
        commands=CommandLedger(),
        owner_action=owner_action,
    )
    registration = start_control_endpoint(
        authority=runtime_authority_v2(),
        dispatcher=dispatcher,
        backend=backend,
    )
    endpoint = list_control_endpoints(backend=backend)[0]
    first = control_relay.unix_connect(
        str(endpoint.socket_path),
        uri="ws://localhost/control",
    )
    second = control_relay.unix_connect(
        str(endpoint.socket_path),
        uri="ws://localhost/control",
    )
    try:
        _attach(first, "attach-first")
        _attach(second, "attach-second")
        acquired = _rpc(
            first,
            request_id="acquire-first",
            method="session.control.acquire",
            params={
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
                "runtime_session_id": "runtime-1",
            },
        )
        first_lease = acquired["result"]["lease_id"]

        conflict = _rpc(
            second,
            request_id="acquire-conflict",
            method="session.control.acquire",
            params={
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
                "runtime_session_id": "runtime-1",
            },
        )
        assert conflict["error"] == {
            "code": 4203,
            "message": "controller_conflict",
        }

        prompt_params = {
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
            "runtime_session_id": "runtime-1",
            "lease_id": first_lease,
            "client_request_id": "request-1",
            "client_turn_id": "client-turn-1",
            "text": "Continue the current task.",
        }
        first_result = _rpc(
            first,
            request_id="prompt-first",
            method="prompt.submit",
            params=prompt_params,
        )
        replay = _rpc(
            first,
            request_id="prompt-replay",
            method="prompt.submit",
            params=prompt_params,
        )
        assert first_result["result"] == replay["result"]
        assert owner_calls == ["prompt.submit"]

        first.close()
        rebound = None
        deadline = time.monotonic() + 1
        attempt = 0
        while rebound is None and time.monotonic() < deadline:
            attempt += 1
            candidate = _rpc(
                second,
                request_id=f"acquire-rebound-{attempt}",
                method="session.control.acquire",
                params={
                    "session_key": "durable-root-1",
                    "runtime_generation": "runtime-generation-1",
                    "runtime_session_id": "runtime-1",
                },
            )
            if "result" in candidate:
                rebound = candidate["result"]
            else:
                assert candidate["error"]["code"] == 4203
                time.sleep(0.01)

        assert rebound is not None
        assert rebound["lease_id"] == "lease-secret-2"
        assert rebound["lease_id"] != first_lease

        stale = _rpc(
            second,
            request_id="renew-stale",
            method="session.control.renew",
            params={
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
                "runtime_session_id": "runtime-1",
                "lease_id": first_lease,
            },
        )
        assert stale["error"] == {
            "code": 4206,
            "message": "lease_mismatch",
        }
    finally:
        first.close()
        second.close()
        registration.close()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            current_workers = {
                thread.ident
                for thread in threading.enumerate()
                if thread.name.startswith("control-owner-action-")
            }
            if not current_workers - baseline_workers:
                break
            time.sleep(0.01)
        assert not current_workers - baseline_workers
