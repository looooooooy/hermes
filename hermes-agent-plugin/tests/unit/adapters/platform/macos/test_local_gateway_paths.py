"""Production path contract shared by every macOS Local Gateway role."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

from hermes_agent_plugin.adapters.platform import macos
from hermes_agent_plugin.adapters.platform.macos import (
    control_relay,
    local_gateway_paths,
    local_relay,
    observer_relay,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.bootstrap import platform_adapters
from hermes_agent_plugin.ports.local_relay import get_local_relay_backend

_REAL_TEMPORARY_DIRECTORY = Path("/tmp").resolve(strict=True)


def test_platform_exposes_explicit_six_path_production_contract(tmp_path: Path) -> None:
    loader = getattr(macos, "load_local_gateway_paths", None)

    assert callable(loader), "macOS adapters must expose one production path loader"

    hermes_home = tmp_path / ".hermes"
    temporary_directory = _REAL_TEMPORARY_DIRECTORY / "hap-contract"
    paths = loader(
        environment={},
        hermes_home=hermes_home,
        temporary_directory=temporary_directory,
        effective_uid=501,
    )

    assert paths.local_gateway_registry_directory == (
        hermes_home / "runtime" / "local-gateways"
    )
    assert paths.local_gateway_socket_directory == (
        temporary_directory / "hermes-local-gateway-501"
    ).resolve(strict=False)
    assert paths.control_registry_directory == (
        hermes_home / "runtime" / "control-gateways"
    )
    assert paths.control_socket_directory == (
        _REAL_TEMPORARY_DIRECTORY / "hermes-control-501"
    )
    assert paths.observer_registry_directory == (
        hermes_home / "runtime" / "observer-gateways"
    )
    assert paths.observer_socket_directory == (
        _REAL_TEMPORARY_DIRECTORY / "hermes-observer-501"
    )
    assert len(set(paths.registry_directories)) == 3
    assert len(set(paths.socket_directories)) == 3


def test_every_role_adapter_consumes_the_shared_path_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-local-sockets",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-control-sockets",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-observer-sockets",
    )
    monkeypatch.setattr(
        local_gateway_paths,
        "load_local_gateway_paths",
        lambda: paths,
    )

    generic_resource = create_local_gateway_resource(
        paths=paths,
        authority=runtime_authority_v2(),
        hello_handler=lambda _value: "{}",
    )
    generic = generic_resource._settings

    assert generic.registry_directory == paths.local_gateway_registry_directory
    assert generic.socket_directory == paths.local_gateway_socket_directory
    assert control_relay._registry_dir() == paths.control_registry_directory
    assert control_relay._socket_dir() == paths.control_socket_directory
    assert observer_relay._registry_dir() == paths.observer_registry_directory
    assert observer_relay._socket_dir() == paths.observer_socket_directory


def test_generic_production_factory_rejects_explicit_path_pair(tmp_path: Path) -> None:
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-local-sockets",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-control-sockets",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-observer-sockets",
    )
    assert not hasattr(macos, "MacOSLocalGatewaySettings")
    resource = create_local_gateway_resource(
        paths=paths,
        authority=runtime_authority_v2(),
        hello_handler=lambda _value: "{}",
    )
    assert resource._settings.registry_directory == (
        paths.local_gateway_registry_directory
    )
    assert resource._settings.socket_directory == paths.local_gateway_socket_directory
    with pytest.raises(TypeError, match="unexpected keyword argument 'settings'"):
        create_local_gateway_resource(
            settings=object(),
            authority=runtime_authority_v2(),
            hello_handler=lambda _value: "{}",
        )


@pytest.mark.parametrize(
    ("relay", "expected"),
    [
        (
            control_relay,
            ("control_registry_directory", "control_socket_directory"),
        ),
        (
            observer_relay,
            ("observer_registry_directory", "observer_socket_directory"),
        ),
    ],
)
def test_role_pair_resolves_one_path_contract(
    relay,
    expected: tuple[str, str],
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-local-sockets",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-control-sockets",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-observer-sockets",
    )
    calls = 0

    def load_paths():
        nonlocal calls
        calls += 1
        return paths

    monkeypatch.setattr(local_gateway_paths, "load_local_gateway_paths", load_paths)

    registry, socket = relay._directories()

    assert calls == 1
    assert registry == getattr(paths, expected[0])
    assert socket == getattr(paths, expected[1])


@pytest.mark.parametrize("configured_leaf", [False, True])
def test_path_contract_rejects_existing_symlink_components(
    tmp_path: Path,
    configured_leaf: bool,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    configured_path = alias_root if configured_leaf else alias_root / "registry"

    with pytest.raises(ValueError, match="symlink"):
        local_gateway_paths.load_local_gateway_paths(
            environment={
                "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": str(configured_path),
            },
            hermes_home=tmp_path / "hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY / "hap-canonical",
            effective_uid=501,
        )


def test_path_contract_rejects_parent_traversal_before_filesystem_side_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="parent traversal"):
        local_gateway_paths.load_local_gateway_paths(
            environment={
                "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": str(marker / ".." / "registry"),
            },
            hermes_home=tmp_path / "hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY / "hap-traversal",
            effective_uid=501,
        )

    assert marker.exists() is False


def test_longest_generic_socket_path_boundary_and_plus_one() -> None:
    boundary = Path("/" + "s" * 71)
    too_long = Path("/" + "s" * 72)
    values = {
        "local_gateway_registry_directory": _REAL_TEMPORARY_DIRECTORY / "hap-registry",
        "control_registry_directory": _REAL_TEMPORARY_DIRECTORY
        / "hap-control-registry",
        "control_socket_directory": _REAL_TEMPORARY_DIRECTORY / "hap-control-sockets",
        "observer_registry_directory": _REAL_TEMPORARY_DIRECTORY
        / "hap-observer-registry",
        "observer_socket_directory": _REAL_TEMPORARY_DIRECTORY / "hap-observer-sockets",
    }

    accepted = macos.MacOSLocalGatewayPaths(
        local_gateway_socket_directory=boundary,
        **values,
    )
    assert len(bytes(accepted.local_gateway_socket_directory)) == 72

    with pytest.raises(ValueError, match="final path"):
        macos.MacOSLocalGatewayPaths(
            local_gateway_socket_directory=too_long,
            **values,
        )


def _absolute_path_with_encoded_length(length: int) -> Path:
    value = "/"
    while len(value.encode()) < length:
        if value != "/":
            value += "/"
        remaining = length - len(value.encode())
        value += "r" * min(200, remaining)
    assert len(value.encode()) == length
    return Path(value)


def _descendant_with_encoded_length(base: Path, length: int) -> Path:
    value = base.resolve(strict=False)
    while len(os.fsencode(value)) < length:
        remaining = length - len(os.fsencode(value)) - 1
        assert remaining > 0
        value /= "r" * min(100, remaining)
    assert len(os.fsencode(value)) == length
    return value


def test_longest_descriptor_temporary_path_boundary_and_plus_one() -> None:
    longest_name = max(
        (
            local_gateway_paths._GENERIC_TEMP_NAME,
            local_gateway_paths._RELAY_TEMP_NAME,
        ),
        key=lambda value: len(os.fsencode(value)),
    )
    boundary_length = 1023 - 1 - len(os.fsencode(longest_name))
    boundary = _absolute_path_with_encoded_length(boundary_length)
    too_long = _absolute_path_with_encoded_length(boundary_length + 1)
    values = {
        "local_gateway_socket_directory": _REAL_TEMPORARY_DIRECTORY
        / "hap-boundary-local",
        "control_registry_directory": _REAL_TEMPORARY_DIRECTORY
        / "hap-boundary-control-registry",
        "control_socket_directory": _REAL_TEMPORARY_DIRECTORY / "hap-boundary-control",
        "observer_registry_directory": _REAL_TEMPORARY_DIRECTORY
        / "hap-boundary-observer-registry",
        "observer_socket_directory": _REAL_TEMPORARY_DIRECTORY
        / "hap-boundary-observer",
    }

    accepted = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=boundary,
        **values,
    )
    assert len(bytes(accepted.local_gateway_registry_directory)) == boundary_length

    with pytest.raises(ValueError, match="final path"):
        macos.MacOSLocalGatewayPaths(
            local_gateway_registry_directory=too_long,
            **values,
        )


def test_real_multicomponent_descriptor_path_max_boundary(
    tmp_path: Path,
) -> None:
    longest_name = max(
        (
            local_gateway_paths._GENERIC_TEMP_NAME,
            local_gateway_paths._RELAY_TEMP_NAME,
        ),
        key=lambda value: len(os.fsencode(value)),
    )
    boundary_length = 1023 - 1 - len(os.fsencode(longest_name))
    boundary = _descendant_with_encoded_length(tmp_path / "boundary", boundary_length)
    socket_directories = (
        _REAL_TEMPORARY_DIRECTORY / f"hap-real-boundary-{os.getpid()}-local",
        _REAL_TEMPORARY_DIRECTORY / f"hap-real-boundary-{os.getpid()}-control",
        _REAL_TEMPORARY_DIRECTORY / f"hap-real-boundary-{os.getpid()}-observer",
    )
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=boundary,
        local_gateway_socket_directory=socket_directories[0],
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=socket_directories[1],
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=socket_directories[2],
    )

    try:
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)
        assert boundary.is_dir()
    finally:
        for directory in socket_directories:
            directory.resolve(strict=False).rmdir()


def test_component_name_max_rejected_before_any_directory_creation(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"
    too_long_component = "x" * (os.pathconf(tmp_path, "PC_NAME_MAX") + 1)

    with pytest.raises(ValueError, match="NAME_MAX"):
        paths = macos.MacOSLocalGatewayPaths(
            local_gateway_registry_directory=marker / "local",
            local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-name-local",
            control_registry_directory=marker / "control",
            control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-name-control",
            observer_registry_directory=tmp_path / too_long_component / "observer",
            observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-name-observer",
        )
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

    assert marker.exists() is False


def test_created_path_contract_rejects_duplicate_device_inode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry_root = tmp_path / "created-registry-root"
    socket_root = _REAL_TEMPORARY_DIRECTORY / f"hap-inode-{os.getpid()}"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=registry_root / "local",
        local_gateway_socket_directory=socket_root / "local",
        control_registry_directory=registry_root / "control",
        control_socket_directory=socket_root / "control",
        observer_registry_directory=registry_root / "observer",
        observer_socket_directory=socket_root / "observer",
    )
    real_identity = local_gateway_paths._directory_identity

    def aliased_identity(directory: Path) -> tuple[int, int]:
        if directory in {
            paths.local_gateway_registry_directory,
            paths.control_registry_directory,
        }:
            return 7, 11
        return real_identity(directory)

    monkeypatch.setattr(
        local_gateway_paths,
        "_directory_identity",
        aliased_identity,
    )

    with pytest.raises(
        ValueError,
        match="physical directories must be distinct",
    ):
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

    assert registry_root.exists() is False
    assert socket_root.exists() is False
    assert all(directory.exists() is False for directory in paths.registry_directories)
    assert all(directory.exists() is False for directory in paths.socket_directories)


def test_rollback_never_deletes_a_replacement_for_an_owned_leaf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry_root = tmp_path / "replacement-registry-root"
    socket_root = _REAL_TEMPORARY_DIRECTORY / f"hap-replacement-{os.getpid()}"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=registry_root / "local",
        local_gateway_socket_directory=socket_root / "local",
        control_registry_directory=registry_root / "control",
        control_socket_directory=socket_root / "control",
        observer_registry_directory=registry_root / "observer",
        observer_socket_directory=socket_root / "observer",
    )
    attacked_leaf = paths.observer_registry_directory
    parked_owned_leaf = registry_root / "observer-owned-by-plugin"
    real_identity = local_gateway_paths._directory_identity
    real_mkdir = os.mkdir
    real_rename = os.rename
    real_rmdir = os.rmdir
    attack_performed = False

    def aliased_identity(directory: Path) -> tuple[int, int]:
        if directory in {
            paths.local_gateway_registry_directory,
            paths.control_registry_directory,
        }:
            return 7, 11
        return real_identity(directory)

    def replace_owned_leaf() -> None:
        nonlocal attack_performed
        attack_performed = True
        real_rename(attacked_leaf, parked_owned_leaf)
        real_mkdir(attacked_leaf, 0o700)

    def attacking_rename(
        source,
        destination,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
    ) -> None:
        if not attack_performed and Path(source).name == attacked_leaf.name:
            replace_owned_leaf()
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def attacking_rmdir(path, *, dir_fd=None) -> None:
        if not attack_performed and Path(path) == attacked_leaf:
            replace_owned_leaf()
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(local_gateway_paths, "_directory_identity", aliased_identity)
    monkeypatch.setattr(os, "rename", attacking_rename)
    monkeypatch.setattr(os, "rmdir", attacking_rmdir)

    with pytest.raises(ValueError, match="physical directories must be distinct"):
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

    assert attack_performed
    assert attacked_leaf.is_dir(), "rollback deleted the replacement directory"
    assert parked_owned_leaf.is_dir(), "attacker-controlled move lost the owned inode"


def test_final_descriptor_identity_rejects_a_swapped_public_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry_root = tmp_path / "final-swap-registry-root"
    socket_root = _REAL_TEMPORARY_DIRECTORY / f"hap-final-swap-{os.getpid()}"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=registry_root / "local",
        local_gateway_socket_directory=socket_root / "local",
        control_registry_directory=registry_root / "control",
        control_socket_directory=socket_root / "control",
        observer_registry_directory=registry_root / "observer",
        observer_socket_directory=socket_root / "observer",
    )
    attacked_leaf = paths.observer_registry_directory
    parked_owned_leaf = registry_root / "observer-owned-by-plugin"
    real_identity = local_gateway_paths._directory_identity
    real_mkdir = os.mkdir
    real_open = os.open
    real_rename = os.rename
    target_open_calls = 0
    attacked = False
    owned_identity: tuple[int, int] | None = None
    replacement_identity: tuple[int, int] | None = None

    def swap_final_leaf() -> None:
        nonlocal attacked, owned_identity, replacement_identity
        attacked = True
        owned_identity = real_identity(attacked_leaf)
        real_rename(attacked_leaf, parked_owned_leaf)
        real_mkdir(attacked_leaf, 0o700)
        replacement_identity = real_identity(attacked_leaf)

    def is_attacked_parent(descriptor: int | None) -> bool:
        if descriptor is None or not registry_root.exists():
            return False
        parent = os.fstat(descriptor)
        expected = registry_root.stat()
        return (parent.st_dev, parent.st_ino) == (expected.st_dev, expected.st_ino)

    def attacking_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal target_open_calls
        if Path(path).name == attacked_leaf.name and is_attacked_parent(dir_fd):
            target_open_calls += 1
            if target_open_calls == 3 and not attacked:
                swap_final_leaf()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def attacking_pathname_identity(directory: Path) -> tuple[int, int]:
        if directory == attacked_leaf and not attacked:
            swap_final_leaf()
        return real_identity(directory)

    monkeypatch.setattr(os, "open", attacking_open)
    monkeypatch.setattr(
        local_gateway_paths,
        "_directory_identity",
        attacking_pathname_identity,
    )

    try:
        with pytest.raises(ValueError, match="changed during creation"):
            local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

        assert attacked
        assert real_identity(attacked_leaf) == replacement_identity
        assert real_identity(parked_owned_leaf) == owned_identity
    finally:
        shutil.rmtree(registry_root, ignore_errors=True)
        shutil.rmtree(socket_root, ignore_errors=True)


@pytest.mark.parametrize("failing_operation", ["fstat", "dup"])
def test_creation_fault_closes_local_fds_and_rolls_back(
    tmp_path: Path,
    monkeypatch,
    failing_operation: str,
) -> None:
    marker = tmp_path / f"fault-{failing_operation}"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=marker / "local",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY
        / f"hap-fault-{failing_operation}-{os.getpid()}-local",
        control_registry_directory=marker / "control",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY
        / f"hap-fault-{failing_operation}-{os.getpid()}-control",
        observer_registry_directory=marker / "observer",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY
        / f"hap-fault-{failing_operation}-{os.getpid()}-observer",
    )
    real_operation = getattr(os, failing_operation)
    real_fstat = os.fstat
    calls = 0

    def live_descriptors() -> set[int]:
        descriptors: set[int] = set()
        for value in os.listdir("/dev/fd"):
            descriptor = int(value)
            try:
                real_fstat(descriptor)
            except OSError:
                continue
            descriptors.add(descriptor)
        return descriptors

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(f"injected {failing_operation} failure")
        return real_operation(*args, **kwargs)

    descriptors_before = live_descriptors()
    monkeypatch.setattr(os, failing_operation, fail_once)
    descriptors_after = descriptors_before
    try:
        with pytest.raises(OSError, match=f"injected {failing_operation} failure"):
            local_gateway_paths.ensure_distinct_local_gateway_directories(paths)
        descriptors_after = live_descriptors()

        assert descriptors_after == descriptors_before
        assert marker.exists() is False
    finally:
        for leaked_descriptor in descriptors_after - descriptors_before:
            os.close(leaked_descriptor)


def test_directory_creation_fails_closed_without_descriptor_relative_support(
    tmp_path: Path,
    monkeypatch,
) -> None:
    marker = tmp_path / "must-not-be-created"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=marker / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-no-dirfd-local",
        control_registry_directory=marker / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-no-dirfd-control",
        observer_registry_directory=marker / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-no-dirfd-observer",
    )
    monkeypatch.setattr(
        local_gateway_paths,
        "_HAS_DESCRIPTOR_RELATIVE_DIRECTORIES",
        False,
    )

    with pytest.raises(RuntimeError, match="descriptor-relative"):
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

    assert marker.exists() is False


def test_one_hundred_failed_directory_transactions_do_not_leak_fds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    real_identity = local_gateway_paths._directory_identity

    def aliased_identity(directory: Path) -> tuple[int, int]:
        if directory.name in {"local", "control"}:
            return 7, 11
        return real_identity(directory)

    monkeypatch.setattr(local_gateway_paths, "_directory_identity", aliased_identity)
    descriptors_before = len(os.listdir("/dev/fd"))

    for cycle in range(100):
        registry_root = tmp_path / f"fd-registry-{cycle}"
        socket_root = _REAL_TEMPORARY_DIRECTORY / f"hap-fd-{os.getpid()}-{cycle}"
        paths = macos.MacOSLocalGatewayPaths(
            local_gateway_registry_directory=registry_root / "local",
            local_gateway_socket_directory=socket_root / "local",
            control_registry_directory=registry_root / "control",
            control_socket_directory=socket_root / "control",
            observer_registry_directory=registry_root / "observer",
            observer_socket_directory=socket_root / "observer",
        )

        with pytest.raises(ValueError, match="physical directories must be distinct"):
            local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

        assert registry_root.exists() is False
        assert socket_root.exists() is False

    assert len(os.listdir("/dev/fd")) == descriptors_before


def test_case_insensitive_alias_rejected_before_directory_creation(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "case-contract"
    socket_root = _REAL_TEMPORARY_DIRECTORY / f"hap-case-{os.getpid()}"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=registry_root / "Role",
        local_gateway_socket_directory=socket_root / "local",
        control_registry_directory=registry_root / "role",
        control_socket_directory=socket_root / "control",
        observer_registry_directory=registry_root / "observer",
        observer_socket_directory=socket_root / "observer",
    )

    with pytest.raises(ValueError, match="case-insensitive aliases"):
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

    assert registry_root.exists() is False
    assert socket_root.exists() is False


def test_existing_physical_alias_rejected_before_directory_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    existing_local = tmp_path / "existing-local"
    existing_control = tmp_path / "existing-control"
    existing_local.mkdir(mode=0o700)
    existing_control.mkdir(mode=0o700)
    created_root = tmp_path / "must-not-be-created"
    socket_root = _REAL_TEMPORARY_DIRECTORY / f"hap-existing-{os.getpid()}"
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=existing_local,
        local_gateway_socket_directory=socket_root / "local",
        control_registry_directory=existing_control,
        control_socket_directory=socket_root / "control",
        observer_registry_directory=created_root / "observer",
        observer_socket_directory=socket_root / "observer",
    )
    real_identity = local_gateway_paths._directory_identity

    def aliased_existing_identity(directory: Path) -> tuple[int, int]:
        if directory in {existing_local, existing_control}:
            return 17, 23
        return real_identity(directory)

    monkeypatch.setattr(
        local_gateway_paths,
        "_directory_identity",
        aliased_existing_identity,
    )

    with pytest.raises(ValueError, match="physical directories must be distinct"):
        local_gateway_paths.ensure_distinct_local_gateway_directories(paths)

    assert existing_local.is_dir()
    assert existing_control.is_dir()
    assert created_root.exists() is False
    assert socket_root.exists() is False


def test_composed_backend_injects_one_immutable_snapshot_into_every_operation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-snapshot-local",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-snapshot-control",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-snapshot-observer",
    )
    observed: list[tuple[str, object]] = []

    class RecordingProvision:
        def __init__(self, value: object) -> None:
            self._value = value

        def __enter__(self) -> object:
            observed.append(("ensure", self._value))
            return self._value

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        local_relay,
        "provision_distinct_local_gateway_directories",
        RecordingProvision,
    )
    monkeypatch.setattr(
        control_relay,
        "start_control_endpoint",
        lambda **kwargs: observed.append(("control-start", kwargs["paths"])),
    )
    monkeypatch.setattr(
        control_relay,
        "list_control_endpoints",
        lambda **kwargs: observed.append(("control-list", kwargs["paths"])) or [],
    )
    monkeypatch.setattr(
        control_relay,
        "ControlRelayHub",
        lambda **kwargs: observed.append(("control-hub", kwargs["paths"])),
    )
    monkeypatch.setattr(
        observer_relay,
        "start_observer_endpoint",
        lambda **kwargs: observed.append(("observer-start", kwargs["paths"])),
    )
    monkeypatch.setattr(
        observer_relay,
        "list_observer_endpoints",
        lambda **kwargs: observed.append(("observer-list", kwargs["paths"])) or [],
    )
    monkeypatch.setattr(
        observer_relay,
        "ObserverRelayHub",
        lambda **kwargs: observed.append(("observer-hub", kwargs["paths"])),
    )

    backend = local_relay.MacOSLocalRelayBackend(paths)
    authority = runtime_authority_v2()
    backend.start_control_endpoint(authority=authority, dispatcher=lambda *_: None)
    backend.list_control_endpoints()
    backend.create_control_relay_hub(current_pid=None)
    backend.start_observer_endpoint(
        authority=authority,
        dispatch=lambda *_: None,
        remove_observer_subscriptions=lambda *_: None,
    )
    backend.list_observer_endpoints()
    backend.create_observer_relay_hub(current_pid=None)

    assert observed == [
        ("ensure", paths),
        ("control-start", paths),
        ("control-list", paths),
        ("control-hub", paths),
        ("ensure", paths),
        ("observer-start", paths),
        ("observer-list", paths),
        ("observer-hub", paths),
    ]


def test_composed_backend_owns_generic_gateway_resource_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-backend-local",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-backend-control",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-backend-observer",
    )
    events: list[tuple[str, object]] = []

    class RecordingProvision:
        def __init__(self, value: object) -> None:
            self._value = value

        def __enter__(self) -> object:
            events.append(("ensure", self._value))
            return self._value

        def __exit__(self, *_args: object) -> None:
            return None

    class RecordingResource:
        def start(self, deadline: float) -> None:
            events.append(("start", deadline))

        def stop(self, deadline: float) -> None:
            events.append(("stop", deadline))

    authority = runtime_authority_v2()

    def hello_handler(_value: object) -> str:
        return "{}"

    resource = RecordingResource()
    monkeypatch.setattr(
        local_relay,
        "provision_distinct_local_gateway_directories",
        RecordingProvision,
    )
    monkeypatch.setattr(
        local_relay,
        "create_local_gateway_resource",
        lambda **kwargs: (
            events.append(
                (
                    "create",
                    (
                        kwargs["paths"],
                        kwargs["authority"],
                        kwargs["hello_handler"],
                    ),
                )
            )
            or resource
        ),
        raising=False,
    )

    registration = local_relay.MacOSLocalRelayBackend(
        paths
    ).start_local_gateway_endpoint(
        authority=authority,
        hello_handler=hello_handler,
    )
    registration.close()
    registration.close()

    assert [name for name, _value in events] == [
        "ensure",
        "create",
        "start",
        "stop",
    ]
    assert events[1][1] == (paths, authority, hello_handler)


def test_composition_root_resolves_exactly_one_backend_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = macos.MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-root-local",
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-root-control",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=_REAL_TEMPORARY_DIRECTORY / "hap-root-observer",
    )
    calls = 0

    def load_paths():
        nonlocal calls
        calls += 1
        return paths

    monkeypatch.setattr(platform_adapters.sys, "platform", "darwin")
    monkeypatch.setattr(local_gateway_paths, "load_local_gateway_paths", load_paths)

    platform_adapters.configure_platform_adapters()
    first = get_local_relay_backend()
    second = get_local_relay_backend()

    assert calls == 1
    assert first is second
    assert first._paths is paths


def test_production_contract_rejects_registry_schema_mixing(tmp_path: Path) -> None:
    shared_registry = _REAL_TEMPORARY_DIRECTORY / "hap-shared-registry"

    with pytest.raises(ValueError, match="registry directories must be distinct"):
        macos.load_local_gateway_paths(
            environment={
                "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": str(shared_registry),
                "HERMES_CONTROL_REGISTRY_DIR": str(shared_registry),
            },
            hermes_home=tmp_path / ".hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY,
            effective_uid=501,
        )


def test_production_contract_rejects_socket_role_mixing(tmp_path: Path) -> None:
    shared_socket_directory = _REAL_TEMPORARY_DIRECTORY / "hap-shared-sockets"

    with pytest.raises(ValueError, match="socket directories must be distinct"):
        macos.load_local_gateway_paths(
            environment={
                "HERMES_CONTROL_SOCKET_DIR": str(shared_socket_directory),
                "HERMES_OBSERVER_SOCKET_DIR": str(shared_socket_directory),
            },
            hermes_home=tmp_path / ".hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY,
            effective_uid=501,
        )


def test_production_contract_rejects_cross_role_path_mixing(tmp_path: Path) -> None:
    shared_directory = _REAL_TEMPORARY_DIRECTORY / "hap-cross-role"

    with pytest.raises(ValueError, match="all six directories must be distinct"):
        macos.load_local_gateway_paths(
            environment={
                "HERMES_CONTROL_REGISTRY_DIR": str(shared_directory),
                "HERMES_OBSERVER_SOCKET_DIR": str(shared_directory),
            },
            hermes_home=tmp_path / ".hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY,
            effective_uid=501,
        )


@pytest.mark.parametrize(
    "environment_key",
    (
        "HERMES_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONTROL_REGISTRY_DIR",
        "HERMES_CONTROL_SOCKET_DIR",
        "HERMES_OBSERVER_REGISTRY_DIR",
        "HERMES_OBSERVER_SOCKET_DIR",
    ),
)
def test_production_contract_rejects_relative_configured_paths(
    environment_key: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        macos.load_local_gateway_paths(
            environment={environment_key: "relative/runtime"},
            hermes_home=tmp_path / ".hermes",
            temporary_directory=tmp_path / "tmp",
            effective_uid=501,
        )


@pytest.mark.parametrize(
    "environment_key",
    (
        "HERMES_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONTROL_REGISTRY_DIR",
        "HERMES_CONTROL_SOCKET_DIR",
        "HERMES_OBSERVER_REGISTRY_DIR",
        "HERMES_OBSERVER_SOCKET_DIR",
    ),
)
def test_production_contract_rejects_explicit_empty_paths(
    environment_key: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        macos.load_local_gateway_paths(
            environment={environment_key: ""},
            hermes_home=tmp_path / ".hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY,
            effective_uid=501,
        )


@pytest.mark.parametrize(
    "environment_key",
    (
        "HERMES_LOCAL_GATEWAY_REGISTRY_DIR",
        "HERMES_LOCAL_GATEWAY_SOCKET_DIR",
        "HERMES_CONTROL_REGISTRY_DIR",
        "HERMES_CONTROL_SOCKET_DIR",
        "HERMES_OBSERVER_REGISTRY_DIR",
        "HERMES_OBSERVER_SOCKET_DIR",
    ),
)
def test_production_contract_rejects_nul_in_every_path(
    environment_key: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must not contain NUL"):
        macos.load_local_gateway_paths(
            environment={environment_key: "/tmp/invalid\x00path"},
            hermes_home=tmp_path / ".hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY,
            effective_uid=501,
        )


@pytest.mark.parametrize(
    ("environment_key", "configured_path"),
    (
        ("HERMES_LOCAL_GATEWAY_REGISTRY_DIR", "/" + "r" * 1024),
        ("HERMES_CONTROL_REGISTRY_DIR", "/" + "r" * 1024),
        ("HERMES_OBSERVER_REGISTRY_DIR", "/" + "r" * 1024),
        ("HERMES_LOCAL_GATEWAY_SOCKET_DIR", "/" + "s" * 76),
        ("HERMES_CONTROL_SOCKET_DIR", "/" + "s" * 76),
        ("HERMES_OBSERVER_SOCKET_DIR", "/" + "s" * 76),
    ),
)
def test_production_contract_rejects_overlong_role_paths(
    environment_key: str,
    configured_path: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="final path exceeds safe length|exceeds NAME_MAX",
    ):
        macos.load_local_gateway_paths(
            environment={environment_key: configured_path},
            hermes_home=tmp_path / ".hermes",
            temporary_directory=_REAL_TEMPORARY_DIRECTORY,
            effective_uid=501,
        )


def test_profile_scoped_hermes_home_uses_shared_runtime_root() -> None:
    paths = macos.load_local_gateway_paths(
        environment={
            "HERMES_HOME": "/srv/hermes/profiles/work",
        },
        temporary_directory=_REAL_TEMPORARY_DIRECTORY,
        effective_uid=501,
    )

    assert paths.local_gateway_registry_directory == Path(
        "/srv/hermes/runtime/local-gateways"
    )
    assert paths.control_registry_directory == Path(
        "/srv/hermes/runtime/control-gateways"
    )
    assert paths.observer_registry_directory == Path(
        "/srv/hermes/runtime/observer-gateways"
    )


def test_all_six_environment_overrides_map_to_matching_contract_fields() -> None:
    environment = {
        "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": "/srv/hap/local-registry",
        "HERMES_LOCAL_GATEWAY_SOCKET_DIR": str(
            _REAL_TEMPORARY_DIRECTORY / "hap-local-sockets"
        ),
        "HERMES_CONTROL_REGISTRY_DIR": "/srv/hap/control-registry",
        "HERMES_CONTROL_SOCKET_DIR": str(
            _REAL_TEMPORARY_DIRECTORY / "hap-control-sockets"
        ),
        "HERMES_OBSERVER_REGISTRY_DIR": "/srv/hap/observer-registry",
        "HERMES_OBSERVER_SOCKET_DIR": str(
            _REAL_TEMPORARY_DIRECTORY / "hap-observer-sockets"
        ),
    }

    paths = macos.load_local_gateway_paths(environment=environment)

    assert paths.local_gateway_registry_directory == Path(
        environment["HERMES_LOCAL_GATEWAY_REGISTRY_DIR"]
    ).resolve(strict=False)
    assert paths.local_gateway_socket_directory == Path(
        environment["HERMES_LOCAL_GATEWAY_SOCKET_DIR"]
    ).resolve(strict=False)
    assert paths.control_registry_directory == Path(
        environment["HERMES_CONTROL_REGISTRY_DIR"]
    ).resolve(strict=False)
    assert paths.control_socket_directory == Path(
        environment["HERMES_CONTROL_SOCKET_DIR"]
    ).resolve(strict=False)
    assert paths.observer_registry_directory == Path(
        environment["HERMES_OBSERVER_REGISTRY_DIR"]
    ).resolve(strict=False)
    assert paths.observer_socket_directory == Path(
        environment["HERMES_OBSERVER_SOCKET_DIR"]
    ).resolve(strict=False)
