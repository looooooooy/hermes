from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from hermes_agent_plugin.adapters.platform.macos.update_safety_relay import (
    MacOSUpdateSafetyRelay,
    resolve_update_safety_socket_path,
)

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="Unix domain sockets are unavailable",
)


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile": "default",
        "runtime_generation": "generation-42",
        "active_tasks": 0,
        "pending_approvals": 0,
        "pending_clarifications": 0,
        "evidence_complete": True,
    }


def _endpoint(tmp_path: Path) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory / "host.sock"


def _request(endpoint: Path, payload: object) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(str(endpoint))
        client.sendall(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(512)
            if not chunk:
                break
            response.extend(chunk)
        return json.loads(bytes(response).decode())
    finally:
        client.close()


def test_resolves_stable_default_and_strict_override() -> None:
    assert resolve_update_safety_socket_path(
        {},
        effective_uid=501,
    ) == Path("/tmp/hermes-update-safety-501/host.sock")
    assert resolve_update_safety_socket_path(
        {"HERMES_UPDATE_SAFETY_SOCKET": "/tmp/private/host.sock"}
    ) == Path("/tmp/private/host.sock")

    with pytest.raises(ValueError, match="absolute and canonical"):
        resolve_update_safety_socket_path(
            {"HERMES_UPDATE_SAFETY_SOCKET": "../host.sock"}
        )


def test_serves_only_the_aggregate_snapshot_contract(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    relay = MacOSUpdateSafetyRelay(_snapshot, socket_path=endpoint).start()
    try:
        assert _request(
            endpoint,
            {"schema_version": 1, "method": "update-safety.snapshot"},
        ) == _snapshot()
        assert stat.S_IMODE(endpoint.lstat().st_mode) == 0o600
        assert stat.S_IMODE(endpoint.parent.lstat().st_mode) == 0o700
    finally:
        relay.close()

    assert not endpoint.exists()


def test_malformed_request_returns_body_free_error(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    called = False

    def provider() -> dict[str, object]:
        nonlocal called
        called = True
        return _snapshot()

    relay = MacOSUpdateSafetyRelay(provider, socket_path=endpoint).start()
    try:
        response = _request(
            endpoint,
            {
                "schema_version": 1,
                "method": "update-safety.snapshot",
                "session_key": "must-not-cross-boundary",
            },
        )
    finally:
        relay.close()

    assert response == {"schema_version": 1, "error": "unavailable"}
    assert called is False


def test_provider_failure_never_leaks_exception_text(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)

    def provider() -> object:
        raise RuntimeError("approval body secret")

    relay = MacOSUpdateSafetyRelay(provider, socket_path=endpoint).start()
    try:
        response = _request(
            endpoint,
            {"schema_version": 1, "method": "update-safety.snapshot"},
        )
    finally:
        relay.close()

    assert response == {"schema_version": 1, "error": "unavailable"}
    assert "secret" not in repr(response)


def test_rejects_non_private_parent(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    os.chmod(endpoint.parent, 0o755)

    with pytest.raises(RuntimeError, match="not private"):
        MacOSUpdateSafetyRelay(_snapshot, socket_path=endpoint).start()


def test_second_relay_cannot_take_over_live_socket(tmp_path: Path) -> None:
    endpoint = _endpoint(tmp_path)
    first = MacOSUpdateSafetyRelay(_snapshot, socket_path=endpoint).start()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            MacOSUpdateSafetyRelay(_snapshot, socket_path=endpoint).start()
        assert _request(
            endpoint,
            {"schema_version": 1, "method": "update-safety.snapshot"},
        ) == _snapshot()
    finally:
        first.close()
