"""Run real Plugin writers against public Connector APIs from installed wheels."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import hermes_connector
from hermes_connector.adapters.platform.macos import (
    MacOSAgentDiscovery,
    MacOSLocalGatewayTransport,
    MacOSLocalRuntimePreflight,
    MacOSObserverClient,
    MacOSObserverEndpointDiscovery,
)
from hermes_connector.adapters.platform.macos.plugin_control_relay import (
    MacOSPluginControlRelay,
)
from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)

import hermes_agent_plugin
from hermes_agent_plugin.adapters.platform.macos import (
    MacOSLocalGatewayPaths,
    control_relay,
    create_local_gateway_resource,
    observer_relay,
)
from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    capture_macos_host_authority,
)


def _installation(package: str, module: object) -> dict[str, object]:
    distribution = importlib.metadata.distribution(package)
    direct_url_text = distribution.read_text("direct_url.json")
    direct_url = json.loads(direct_url_text) if direct_url_text else {}
    module_path = Path(module.__file__).resolve()
    environment = Path(sys.prefix).resolve()
    return {
        "editable": direct_url.get("dir_info", {}).get("editable", False),
        "wheel_url": str(direct_url.get("url", "")).endswith(".whl"),
        "inside_environment": module_path.is_relative_to(environment),
    }


def _paths(root: Path) -> MacOSLocalGatewayPaths:
    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=root / "local-registry",
        local_gateway_socket_directory=root / "local-sockets",
        control_registry_directory=root / "control-registry",
        control_socket_directory=root / "control-sockets",
        observer_registry_directory=root / "observer-registry",
        observer_socket_directory=root / "observer-sockets",
    )


def _control_dispatch(request: dict, _transport: object) -> dict[str, object]:
    result: dict[str, object]
    if request.get("method") == "session.control.acquire":
        result = {"lease_id": "wheel-interop-lease"}
    else:
        result = {"accepted": True}
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def _observer_dispatch(request: dict, _transport: object) -> dict[str, object]:
    if request.get("method") == "session.observe.unsubscribe":
        result: dict[str, object] = {"unsubscribed": True}
    else:
        params = request.get("params")
        session_key = params.get("session_key") if isinstance(params, dict) else None
        result = {
            "subscription_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_key": session_key,
            "runtime_session_id": "runtime-session-1",
            "running": True,
            "status": "running",
            "event_sequence": 0,
            "snapshot_event_sequence": 0,
            "messages": [],
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "replay_events": [],
        }
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def _mutate_descriptors(paths: MacOSLocalGatewayPaths, mutation: str) -> None:
    if mutation == "none":
        return
    for directory in paths.registry_directories:
        descriptor_path = next(directory.glob("gateway-*.json"))
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if mutation == "v1":
            descriptor["version"] = 1
        elif mutation == "missing-field":
            descriptor.pop("host_bundle_id")
        elif mutation == "process-evidence-mismatch":
            descriptor["process_executable_inode"] += 1
        else:
            raise ValueError("unknown mutation")
        descriptor_path.write_text(
            json.dumps(descriptor, separators=(",", ":")),
            encoding="utf-8",
        )
        descriptor_path.chmod(0o600)


def _descriptor_summary(paths: MacOSLocalGatewayPaths) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for directory in paths.registry_directories:
        path = next(directory.glob("gateway-*.json"))
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        result.append(
            {
                "fields": sorted(descriptor),
                "version": descriptor.get("version"),
                "mode": path.stat().st_mode & 0o777,
                "socket_inode": Path(descriptor["socket_path"]).lstat().st_ino,
            }
        )
    return result


async def _exercise_connector(
    paths: MacOSLocalGatewayPaths,
    runtime_authority: LocalRuntimeAuthority,
) -> dict[str, object]:
    local_discovery = MacOSAgentDiscovery(
        paths.local_gateway_registry_directory,
        paths.local_gateway_socket_directory,
    )
    local_endpoint = MacOSLocalRuntimePreflight(
        discovery=local_discovery,
        transport=MacOSLocalGatewayTransport(),
        timeout_seconds=1.0,
    ).verify("default")

    async def authority() -> LocalRuntimeAuthority:
        return runtime_authority

    issued_at = datetime.now(UTC)
    command = CommandDelivery(
        command_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        client_instance_id=UUID("22222222-2222-4222-8222-222222222222"),
        session_key="wheel-session-root",
        profile="default",
        client_request_id="wheel-client-request",
        method="session.interrupt",
        params=MappingProxyType(
            {
                "runtime_session_id": "runtime-session-1",
                "runtime_generation": "runtime-generation-1",
            }
        ),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=1),
        revision=1,
    )
    try:
        control_result = await MacOSPluginControlRelay(
            registry_directory=paths.control_registry_directory,
            socket_directory=paths.control_socket_directory,
            profile="default",
            user_id="wheel-interop",
            provider="hermes-cloud",
            authority=authority,
        ).execute(command)
        control_ok = control_result == {"accepted": True}
    except Exception:
        control_ok = False

    observer_client = MacOSObserverClient(
        discovery=MacOSObserverEndpointDiscovery(
            paths.observer_registry_directory,
            paths.observer_socket_directory,
        ),
        authority=authority,
        rpc_timeout_seconds=2.0,
    )
    try:
        try:
            subscription = await observer_client.subscribe(
                profile="default",
                session_key="wheel-session-root",
            )
            observer_ok = subscription.snapshot.session_key == "wheel-session-root"
            await subscription.close()
        except Exception:
            observer_ok = False
    finally:
        await observer_client.aclose()

    return {
        "local": local_endpoint is not None,
        "control": control_ok,
        "observer": observer_ok,
    }


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    mutation = sys.argv[2]
    paths = _paths(root)
    authority = capture_macos_host_authority(
        profile="default",
        host_bundle_id="com.nousresearch.hermes",
    ).bind_runtime("runtime-generation-1")
    runtime_authority = LocalRuntimeAuthority(
        profile=authority.profile,
        runtime_generation=authority.runtime_generation,
        instance_id=authority.instance_id,
        host_bundle_id=authority.host_bundle_id,
        process_identity=ProcessIdentityEvidence(
            start_time_ns=authority.process_identity.start_time_ns,
            executable_path=authority.process_identity.executable_path,
            executable_device=authority.process_identity.executable_device,
            executable_inode=authority.process_identity.executable_inode,
        ),
        required_capabilities=("session.observe",),
        optional_capabilities=("session.control",),
    )
    result: dict[str, object]
    with ExitStack() as stack:
        local = create_local_gateway_resource(
            paths=paths,
            authority=authority,
            hello_handler=lambda _raw: "{}",
        )
        local.start(time.monotonic() + 2.0)
        stack.callback(lambda: local.stop(time.monotonic() + 5.0))
        control = control_relay.start_control_endpoint(
            authority=authority,
            dispatcher=_control_dispatch,
            paths=paths,
        )
        stack.callback(control.close)
        observer = observer_relay.start_observer_endpoint(
            authority=authority,
            dispatch=_observer_dispatch,
            remove_observer_subscriptions=lambda _transport: None,
            paths=paths,
        )
        stack.callback(observer.close)
        _mutate_descriptors(paths, mutation)
        descriptors = _descriptor_summary(paths)
        try:
            result = asyncio.run(_exercise_connector(paths, runtime_authority))
        except Exception as error:
            result = {
                "local": False,
                "control": False,
                "observer": False,
                "error_type": type(error).__name__,
            }

    print(
        json.dumps(
            {
                "installations": {
                    "plugin": _installation(
                        "hermes-agent-plugin",
                        hermes_agent_plugin,
                    ),
                    "connector": _installation("hermes-connector", hermes_connector),
                },
                "result": result,
                "descriptors": descriptors,
                "mutation": mutation,
                "pid": os.getpid(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
