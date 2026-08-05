from __future__ import annotations

import json
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import hermes_connector.adapters.platform.macos.observer_discovery as discovery_module
from hermes_connector.adapters.platform.macos.observer_discovery import (
    MacOSObserverEndpointDiscovery,
)


@dataclass(frozen=True)
class _ProcessIdentity:
    start_time_ns: int = 1_000
    executable_path: Path = Path("/private/fixture/hermes-python")
    executable_device: int = 41
    executable_inode: int = 73
    bundle_id: str = "com.nousresearch.hermes"


def _process_fields() -> dict[str, object]:
    identity = _ProcessIdentity()
    return {
        "process_start_time_ns": identity.start_time_ns,
        "process_executable": str(identity.executable_path),
        "process_executable_device": identity.executable_device,
        "process_executable_inode": identity.executable_inode,
        "host_bundle_id": identity.bundle_id,
    }


def _test_discovery(*args, **kwargs) -> MacOSObserverEndpointDiscovery:
    kwargs.setdefault("process_identity_provider", lambda _: _ProcessIdentity())
    return MacOSObserverEndpointDiscovery(*args, **kwargs)


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


@pytest.fixture
def short_root() -> Path:
    with tempfile.TemporaryDirectory(prefix="hcod-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.asyncio
async def test_observer_v1_descriptor_is_rejected_without_identity_guessing(
    short_root: Path,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "observer.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            }
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda _: True,
    )
    try:
        assert await discovery.discover("default") == ()
    finally:
        await discovery.aclose()
        listener.close()


@pytest.mark.asyncio
async def test_observer_same_numeric_pid_reuse_during_discovery_fails_closed(
    short_root: Path,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "observer.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    expected = _ProcessIdentity()
    descriptor = registry / "gateway.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "process_start_time_ns": expected.start_time_ns,
                "process_executable": str(expected.executable_path),
                "process_executable_device": expected.executable_device,
                "process_executable_inode": expected.executable_inode,
                "host_bundle_id": expected.bundle_id,
            }
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    observed = iter((expected, _ProcessIdentity(start_time_ns=2_000)))
    discovery = MacOSObserverEndpointDiscovery(
        registry,
        sockets,
        pid_is_alive=lambda _: True,
        process_identity_provider=lambda _: next(observed),
    )
    try:
        assert await discovery.discover("default") == ()
    finally:
        await discovery.aclose()
        listener.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_candidates", True),
        ("max_candidates", 1.0),
        ("max_candidates", "1"),
        ("max_descriptor_bytes", True),
        ("max_descriptor_bytes", 1.0),
        ("max_descriptor_bytes", "1"),
    ),
)
def test_observer_discovery_bounds_require_exact_positive_integers(
    field: str,
    value: object,
) -> None:
    kwargs = {field: value}

    with pytest.raises(
        ValueError,
        match="Observer discovery bounds must be positive integers",
    ):
        MacOSObserverEndpointDiscovery(Path("/tmp/r"), Path("/tmp/s"), **kwargs)


@pytest.mark.asyncio
async def test_observer_discovery_consumes_exact_generation_bound_plugin_descriptor(
    short_root: Path,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-101-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway-101-aaaaaaaa.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                **_process_fields(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda pid: pid == 101,
    )
    try:
        endpoints = await discovery.discover("default")

        assert len(endpoints) == 1
        assert endpoints[0].profile == "default"
        assert endpoints[0].runtime_generation == "runtime-generation-1"
        assert endpoints[0].instance_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert endpoints[0].socket_path == socket_path
    finally:
        await discovery.aclose()
        endpoint_socket.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "expected_count"),
    ((True, 0), (False, 0), (1, 0), (0, 0), (2, 1)),
)
async def test_observer_discovery_requires_exact_integer_descriptor_version(
    short_root: Path,
    version: object,
    expected_count: int,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-101-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway-101-aaaaaaaa.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": version,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                **_process_fields(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda _: True,
    )
    try:
        assert len(await discovery.discover("default")) == expected_count
    finally:
        await discovery.aclose()
        endpoint_socket.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        {"runtime_generation": None},
        {"runtime_generation": ""},
        {"runtime_generation": "x" * 129},
        {"instance_id": "not-an-instance"},
        {"instance_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"unexpected": True},
    ),
)
async def test_observer_discovery_rejects_generic_or_nonexact_descriptor(
    short_root: Path,
    mutation: dict[str, object],
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-101-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    value: dict[str, object] = {
        "version": 2,
        "pid": 101,
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "socket_path": str(socket_path),
        "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        **_process_fields(),
    }
    if mutation.get("runtime_generation", object()) is None:
        value.pop("runtime_generation")
    else:
        value.update(mutation)
    descriptor = registry / "gateway-101-aaaaaaaa.json"
    descriptor.write_text(json.dumps(value), encoding="utf-8")
    descriptor.chmod(0o600)
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda _: True,
    )
    try:
        assert await discovery.discover("default") == ()
    finally:
        await discovery.aclose()
        endpoint_socket.close()


@pytest.mark.asyncio
async def test_observer_discovery_rejects_in_place_descriptor_mutation(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-101-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway-101-aaaaaaaa.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                **_process_fields(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    original_read = discovery_module._read

    def mutate_after_read(file_descriptor: int, maximum: int) -> bytes:
        raw = original_read(file_descriptor, maximum)
        with descriptor.open("ab") as mutable:
            mutable.write(b" ")
        return raw

    monkeypatch.setattr(discovery_module, "_read", mutate_after_read)
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda _: True,
    )
    try:
        assert await discovery.discover("default") == ()
    finally:
        await discovery.aclose()
        endpoint_socket.close()


@pytest.mark.asyncio
async def test_stale_observer_descriptor_is_ignored_without_repair_or_deletion(
    short_root: Path,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-50285-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway-50285-aaaaaaaa.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 50285,
                "profile": "default",
                "runtime_generation": "retired-runtime-generation",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                **_process_fields(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda _: False,
    )
    try:
        assert await discovery.discover("default") == ()
        assert descriptor.exists()
        assert socket_path.exists()
    finally:
        await discovery.aclose()
        endpoint_socket.close()


@pytest.mark.asyncio
async def test_observer_candidate_overflow_fails_closed(
    short_root: Path,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-101-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway-000-valid.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                **_process_fields(),
            }
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    extra = registry / "gateway-999-extra.json"
    extra.write_text("{}", encoding="utf-8")
    extra.chmod(0o600)
    discovery = _test_discovery(
        registry,
        sockets,
        max_candidates=1,
        pid_is_alive=lambda _: True,
    )
    try:
        assert await discovery.discover("default") == ()
    finally:
        await discovery.aclose()
        endpoint_socket.close()


@pytest.mark.asyncio
async def test_observer_all_directory_entries_are_bounded(
    short_root: Path,
) -> None:
    registry = _private_directory(short_root / "r")
    sockets = _private_directory(short_root / "s")
    socket_path = sockets / "o-101-aaaaaaaa.sock"
    endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint_socket.bind(str(socket_path))
    socket_path.chmod(0o600)
    descriptor = registry / "gateway-101-aaaaaaaa.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 101,
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                **_process_fields(),
            }
        ),
        encoding="utf-8",
    )
    descriptor.chmod(0o600)
    for index in range(64):
        (registry / f"noise-{index:03d}").write_text("ignored", encoding="utf-8")
    discovery = _test_discovery(
        registry,
        sockets,
        pid_is_alive=lambda _: True,
    )
    try:
        assert await discovery.discover("default") == ()
    finally:
        await discovery.aclose()
        endpoint_socket.close()
