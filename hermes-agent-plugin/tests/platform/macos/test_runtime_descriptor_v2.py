"""Fail-closed shared macOS runtime descriptor v2 evidence."""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import stat
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_agent_plugin.adapters.platform.macos import control_relay, observer_relay
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)


def _module():
    try:
        return importlib.import_module(
            "hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2"
        )
    except ModuleNotFoundError:
        pytest.fail("shared macOS runtime descriptor v2 authority is missing")


def _paths(root: Path) -> MacOSLocalGatewayPaths:
    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=root / "local-registry",
        local_gateway_socket_directory=root / "local-sockets",
        control_registry_directory=root / "control-registry",
        control_socket_directory=root / "control-sockets",
        observer_registry_directory=root / "observer-registry",
        observer_socket_directory=root / "observer-sockets",
    )


def _authority():
    return (
        _module()
        .capture_macos_host_authority(
            profile="default",
            host_bundle_id="com.nousresearch.hermes",
        )
        .bind_runtime("runtime-generation-1")
    )


def _install_fake_libproc(
    monkeypatch,
    module,
    *,
    executable_path: Path,
    start_times: tuple[tuple[int, int], ...],
):
    class FakeFunction:
        def __init__(self, implementation) -> None:
            self._implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self._implementation(*args)

    observed_start_times = iter(start_times)
    info_calls = 0

    def proc_pidinfo(pid, _flavor, _argument, pointer, size):
        nonlocal info_calls
        info_calls += 1
        seconds, microseconds = next(observed_start_times)
        pointer._obj.pbi_pid = pid
        pointer._obj.pbi_start_tvsec = seconds
        pointer._obj.pbi_start_tvusec = microseconds
        return size

    def proc_pidpath(_pid, buffer, _size):
        raw = os.fsencode(executable_path) + b"\x00"
        ctypes.memmove(buffer, raw, len(raw))
        return len(raw)

    library = SimpleNamespace(
        proc_pidinfo=FakeFunction(proc_pidinfo),
        proc_pidpath=FakeFunction(proc_pidpath),
    )
    monkeypatch.setattr(module.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(module.ctypes.util, "find_library", lambda _name: "libproc")
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *_args, **_kwargs: library)
    return lambda: info_calls


def test_process_identity_capture_rejects_mixed_pid_start_snapshots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    executable = tmp_path / "Hermes"
    executable.write_bytes(b"mach-o")
    call_count = _install_fake_libproc(
        monkeypatch,
        module,
        executable_path=executable,
        start_times=((10, 100), (11, 100)),
    )

    assert module.current_process_identity(4242) is None
    assert call_count() == 2


def test_process_identity_capture_rejects_executable_path_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    executable = tmp_path / "Hermes"
    displaced = tmp_path / "Hermes-old"
    executable.write_bytes(b"original")
    _install_fake_libproc(
        monkeypatch,
        module,
        executable_path=executable,
        start_times=((10, 100), (10, 100)),
    )
    real_fstat = module.os.fstat
    replaced = False

    def replace_after_capture(file_descriptor: int):
        nonlocal replaced
        metadata = real_fstat(file_descriptor)
        if not replaced and stat.S_ISREG(metadata.st_mode):
            executable.rename(displaced)
            executable.write_bytes(b"replacement")
            replaced = True
        return metadata

    monkeypatch.setattr(module.os, "fstat", replace_after_capture)

    assert module.current_process_identity(4242) is None
    assert replaced is True


def test_shared_process_identity_conformance_vectors() -> None:
    module = _module()
    fixture = (
        Path(__file__).resolve().parents[4]
        / "contracts/fixtures/conformance/runtime-process-identity-v1.json"
    )
    packet = json.loads(fixture.read_text(encoding="utf-8"))

    assert packet["contract"] == "runtime-process-identity-coherence-v1"
    for vector in packet["vectors"]:
        descriptor = module.normalize_process_identity(
            SimpleNamespace(**vector["descriptor"])
        )
        observed = module.normalize_process_identity(
            SimpleNamespace(**vector["observed"])
        )
        assert descriptor is not None, vector["name"]
        assert observed is not None, vector["name"]
        assert (descriptor == observed) is vector["accepted"], vector["name"]


@pytest.mark.parametrize(
    "role",
    ("local", "control", "observer"),
)
def test_process_evidence_failure_precedes_role_socket_descriptor_and_thread(
    role: str,
    monkeypatch,
) -> None:
    authority = _authority()
    with tempfile.TemporaryDirectory(prefix="hap-v2-fail-", dir="/tmp") as raw_root:
        root = Path(raw_root).resolve()
        paths = _paths(root)
        server_started = False

        def fail_if_server_started(*_args, **_kwargs):
            nonlocal server_started
            server_started = True
            raise AssertionError("server must not start before process evidence")

        if role == "local":
            resource = create_local_gateway_resource(
                paths=paths,
                authority=authority,
                process_identity_provider=lambda _pid: None,
                hello_handler=lambda _raw: "{}",
            )
            with pytest.raises(RuntimeError, match="process identity unavailable"):
                resource.start(time.monotonic() + 1.0)
        elif role == "control":
            monkeypatch.setattr(control_relay, "unix_serve", fail_if_server_started)
            with pytest.raises(RuntimeError, match="process identity unavailable"):
                control_relay.start_control_endpoint(
                    authority=authority,
                    process_identity_provider=lambda _pid: None,
                    dispatcher=lambda _request, _transport: None,
                )
        else:
            monkeypatch.setattr(observer_relay, "unix_serve", fail_if_server_started)
            with pytest.raises(RuntimeError, match="process identity unavailable"):
                observer_relay.start_observer_endpoint(
                    authority=authority,
                    process_identity_provider=lambda _pid: None,
                    dispatch=lambda _request, _transport: None,
                    remove_observer_subscriptions=lambda _transport: None,
                )

        assert server_started is False
        assert tuple(root.iterdir()) == ()


@pytest.mark.parametrize("role", ("control", "observer"))
@pytest.mark.parametrize("identity_state", ("unavailable", "stale"))
def test_production_relay_wrapper_rechecks_authority_before_six_path_effects(
    role: str,
    identity_state: str,
    monkeypatch,
) -> None:
    authority = _authority()
    current_identity = authority.process_identity

    def unavailable_identity(_pid):
        return None

    def captured_current_identity(_pid):
        return current_identity

    if identity_state == "unavailable":
        endpoint_authority = authority
        identity_provider = unavailable_identity
        expected_error = "process identity unavailable"
    else:
        endpoint_authority = replace(
            authority,
            process_identity=replace(
                current_identity,
                start_time_ns=current_identity.start_time_ns + 1_000,
            ),
        )
        identity_provider = captured_current_identity
        expected_error = "process identity mismatch"

    with tempfile.TemporaryDirectory(prefix="hap-v2-wrapper-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        backend = MacOSLocalRelayBackend(_paths(root))
        server_started = False
        baseline_threads = {
            (thread.name, thread.ident) for thread in threading.enumerate()
        }

        def fail_if_server_started(*_args, **_kwargs):
            nonlocal server_started
            server_started = True
            raise AssertionError("server must not start before process evidence")

        if role == "control":
            monkeypatch.setattr(control_relay, "unix_serve", fail_if_server_started)
            with pytest.raises(RuntimeError, match=expected_error):
                backend.start_control_endpoint(
                    authority=endpoint_authority,
                    process_identity_provider=identity_provider,
                    dispatcher=lambda _request, _transport: None,
                )
        else:
            monkeypatch.setattr(observer_relay, "unix_serve", fail_if_server_started)
            with pytest.raises(RuntimeError, match=expected_error):
                backend.start_observer_endpoint(
                    authority=endpoint_authority,
                    process_identity_provider=identity_provider,
                    dispatch=lambda _request, _transport: None,
                    remove_observer_subscriptions=lambda _transport: None,
                )

        assert server_started is False
        assert tuple(root.iterdir()) == ()
        assert {
            (thread.name, thread.ident) for thread in threading.enumerate()
        } == baseline_threads


@pytest.mark.parametrize("role", ("control", "observer"))
@pytest.mark.parametrize("preexisting_directory", (False, True))
def test_production_relay_wrapper_rolls_back_only_new_directories_when_recheck_fails(
    role: str,
    preexisting_directory: bool,
) -> None:
    authority = _authority()
    calls = 0

    def identity_then_unavailable(_pid):
        nonlocal calls
        calls += 1
        return authority.process_identity if calls == 1 else None

    with tempfile.TemporaryDirectory(prefix="hap-v2-transaction-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        paths = _paths(root)
        marker = paths.control_registry_directory / "preexisting.txt"
        if preexisting_directory:
            paths.control_registry_directory.mkdir(mode=0o700)
            marker.write_text("preserve", encoding="utf-8")
            marker.chmod(0o600)

        backend = MacOSLocalRelayBackend(paths)
        with pytest.raises(RuntimeError, match="process identity unavailable"):
            if role == "control":
                backend.start_control_endpoint(
                    authority=authority,
                    process_identity_provider=identity_then_unavailable,
                    dispatcher=lambda _request, _transport: None,
                )
            else:
                backend.start_observer_endpoint(
                    authority=authority,
                    process_identity_provider=identity_then_unavailable,
                    dispatch=lambda _request, _transport: None,
                    remove_observer_subscriptions=lambda _transport: None,
                )

        assert calls == 2
        if preexisting_directory:
            assert tuple(root.iterdir()) == (paths.control_registry_directory,)
            assert marker.read_text(encoding="utf-8") == "preserve"
        else:
            assert tuple(root.iterdir()) == ()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("start_time_ns", lambda value: value + 1_000),
        ("executable_path", lambda _value: Path("/bin/sh")),
        ("executable_device", lambda value: value + 1),
        ("executable_inode", lambda value: value + 1),
    ),
)
def test_each_process_identity_mismatch_fails_before_local_publication(
    field: str,
    replacement,
) -> None:
    authority = _authority()
    process_identity = authority.process_identity
    stale_identity = replace(
        process_identity,
        **{field: replacement(getattr(process_identity, field))},
    )
    stale_authority = replace(authority, process_identity=stale_identity)
    with tempfile.TemporaryDirectory(prefix="hap-v2-mismatch-", dir="/tmp") as raw_root:
        root = Path(raw_root).resolve()
        resource = create_local_gateway_resource(
            paths=_paths(root),
            authority=stale_authority,
            hello_handler=lambda _raw: "{}",
        )

        with pytest.raises(RuntimeError, match="process identity mismatch"):
            resource.start(time.monotonic() + 1.0)

        assert tuple(root.iterdir()) == ()


@pytest.mark.parametrize(
    "socket_path",
    (Path("/"), Path("/tmp") / ("s" * 99)),
    ids=("too-short", "native-uds-overflow"),
)
def test_encoder_rejects_socket_paths_outside_connector_bounds(
    socket_path: Path,
) -> None:
    with pytest.raises(ValueError, match="socket_path is invalid"):
        _module().encode_runtime_descriptor_v2(
            _authority(),
            socket_path=socket_path,
        )


@pytest.mark.parametrize("role", ("local", "control", "observer"))
def test_descriptor_publication_failure_never_starts_role_thread(
    role: str,
    monkeypatch,
) -> None:
    authority = _authority()
    publication_attempted = False
    started_thread_names: list[str] = []

    def fail_publication(**_kwargs):
        nonlocal publication_attempted
        publication_attempted = True
        raise OSError("injected publication failure")

    class RecordingThread:
        def __init__(self, **_kwargs) -> None:
            self.ident = None
            self.name = f"{role}-recording-thread"

        def start(self) -> None:
            started_thread_names.append(self.name)

        def is_alive(self) -> bool:
            return False

        def join(self, **_kwargs) -> None:
            return None

    with tempfile.TemporaryDirectory(prefix="hap-v2-publish-", dir="/tmp") as raw:
        paths = _paths(Path(raw).resolve())
        if role == "local":
            from hermes_agent_plugin.adapters.platform.macos import (
                local_gateway_transport,
            )

            monkeypatch.setattr(
                local_gateway_transport,
                "publish_runtime_descriptor_v2",
                fail_publication,
            )
            monkeypatch.setattr(
                local_gateway_transport.threading,
                "Thread",
                RecordingThread,
            )
            resource = create_local_gateway_resource(
                paths=paths,
                authority=authority,
                hello_handler=lambda _raw: "{}",
            )
            with pytest.raises(OSError, match="injected publication failure"):
                resource.start(time.monotonic() + 2.0)
        elif role == "control":
            monkeypatch.setattr(
                control_relay,
                "publish_runtime_descriptor_v2",
                fail_publication,
            )
            monkeypatch.setattr(control_relay.threading, "Thread", RecordingThread)
            with pytest.raises(OSError, match="injected publication failure"):
                control_relay.start_control_endpoint(
                    authority=authority,
                    dispatcher=lambda _request, _transport: None,
                    paths=paths,
                )
        else:
            monkeypatch.setattr(
                observer_relay,
                "publish_runtime_descriptor_v2",
                fail_publication,
            )
            monkeypatch.setattr(observer_relay.threading, "Thread", RecordingThread)
            with pytest.raises(OSError, match="injected publication failure"):
                observer_relay.start_observer_endpoint(
                    authority=authority,
                    dispatch=lambda _request, _transport: None,
                    remove_observer_subscriptions=lambda _transport: None,
                    paths=paths,
                )

        assert publication_attempted is True
        assert not any(
            name.startswith(("local-gateway-", "control-socket-", "observer-socket-"))
            for name in started_thread_names
        )


def test_local_start_cleanup_retains_nonstopping_thread_for_retry(
    monkeypatch,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos import local_gateway_transport
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        LocalTransportState,
    )

    class NonStoppingThread:
        def __init__(self, **_kwargs) -> None:
            self.name = "local-gateway-nonstopping"
            self.ident = 999
            self.started = False

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return self.started

        def join(self, **_kwargs) -> None:
            return None

    monkeypatch.setattr(
        local_gateway_transport.threading,
        "Thread",
        NonStoppingThread,
    )
    with tempfile.TemporaryDirectory(prefix="hap-v2-retry-", dir="/tmp") as raw:
        resource = create_local_gateway_resource(
            paths=_paths(Path(raw).resolve()),
            authority=_authority(),
            hello_handler=lambda _raw: "{}",
        )
        transition = resource._transition

        def fail_ready(target):
            if target is LocalTransportState.READY:
                raise RuntimeError("injected ready transition failure")
            transition(target)

        monkeypatch.setattr(resource, "_transition", fail_ready)
        with pytest.raises(
            RuntimeError, match="local gateway start cleanup incomplete"
        ):
            resource.start(time.monotonic() + 1.0)

        assert resource.state is LocalTransportState.STOPPING
        retained_thread = resource._thread
        assert retained_thread is not None
        assert retained_thread.is_alive()

        with pytest.raises(Exception, match="lifecycle_deadline_exceeded"):
            resource.stop(time.monotonic() + 0.01)
        assert resource._thread is retained_thread
        assert resource.state is LocalTransportState.STOPPING


def test_publisher_ignores_preexisting_stale_temp_file() -> None:
    module = _module()
    authority = _authority()
    with tempfile.TemporaryDirectory(prefix="hap-v2-stale-", dir="/tmp") as raw:
        registry = Path(raw).resolve() / "registry"
        registry.mkdir(mode=0o700)
        target = registry / "gateway.json"
        stale = registry / f".{target.name}.{authority.instance_id}.tmp"
        stale.write_text("stale", encoding="utf-8")
        stale.chmod(0o600)

        module.publish_runtime_descriptor_v2(
            registry_directory=registry,
            target=target,
            authority=authority,
            socket_path=Path(raw).resolve() / "owner.sock",
        )

        assert target.is_file()
        assert stale.read_text(encoding="utf-8") == "stale"


def test_publisher_dirfd_survives_registry_path_rename_and_replacement(
    monkeypatch,
) -> None:
    module = _module()
    with tempfile.TemporaryDirectory(prefix="hap-v2-dirfd-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        registry = root / "registry"
        moved_registry = root / "registry-moved"
        registry.mkdir(mode=0o700)
        target = registry / "gateway.json"
        real_fsync = module.os.fsync
        swapped = False

        def swap_after_file_fsync(file_descriptor: int) -> None:
            nonlocal swapped
            real_fsync(file_descriptor)
            if swapped:
                return
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return
            registry.rename(moved_registry)
            registry.mkdir(mode=0o700)
            swapped = True

        monkeypatch.setattr(module.os, "fsync", swap_after_file_fsync)
        module.publish_runtime_descriptor_v2(
            registry_directory=registry,
            target=target,
            authority=_authority(),
            socket_path=root / "owner.sock",
        )

        assert swapped is True
        assert (moved_registry / target.name).is_file()
        assert tuple(registry.iterdir()) == ()


@pytest.mark.parametrize("failure_point", ("registry-fstat", "temporary-uuid"))
def test_publisher_releases_registry_fd_when_initialization_fails(
    failure_point: str,
    monkeypatch,
) -> None:
    module = _module()
    authority = _authority()
    with tempfile.TemporaryDirectory(prefix="hap-v2-fd-owner-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        registry = root / "registry"
        registry.mkdir(mode=0o700)
        target = registry / "gateway.json"
        baseline_descriptors = len(os.listdir("/dev/fd"))

        with monkeypatch.context() as failure_patch:
            if failure_point == "registry-fstat":
                failure_patch.setattr(
                    module.os,
                    "fstat",
                    lambda _descriptor: (_ for _ in ()).throw(
                        OSError("injected registry fstat failure")
                    ),
                )
                expected_error = "injected registry fstat failure"
            else:
                failure_patch.setattr(
                    module.uuid,
                    "uuid4",
                    lambda: (_ for _ in ()).throw(
                        RuntimeError("injected temporary uuid failure")
                    ),
                )
                expected_error = "injected temporary uuid failure"

            for _iteration in range(50):
                with pytest.raises(Exception, match=expected_error):
                    module.publish_runtime_descriptor_v2(
                        registry_directory=registry,
                        target=target,
                        authority=authority,
                        socket_path=root / "owner.sock",
                    )

        assert len(os.listdir("/dev/fd")) == baseline_descriptors
        assert tuple(registry.iterdir()) == ()
        assert target.exists() is False
