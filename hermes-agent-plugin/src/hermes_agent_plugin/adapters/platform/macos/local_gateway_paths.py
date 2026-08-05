"""One explicit production path contract for every macOS Local Gateway role."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .local_trust import is_private_directory

_MAX_FILESYSTEM_PATH_BYTES = 1023
_MAX_UDS_PATH_BYTES = 103
_MAX_PID_TEXT = "2147483647"
_MAX_PROFILE_HASH = "f" * 12
_MAX_INSTANCE_HEX = "f" * 32
_MAX_INSTANCE_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_GENERIC_SOCKET_NAME = f"g-{_MAX_PID_TEXT}-{_MAX_PROFILE_HASH}.sock"
_CONTROL_SOCKET_NAME = f"c-{_MAX_PID_TEXT}-{_MAX_INSTANCE_HEX[:8]}.sock"
_OBSERVER_SOCKET_NAME = f"o-{_MAX_PID_TEXT}-{_MAX_INSTANCE_HEX[:8]}.sock"
_GENERIC_DESCRIPTOR_NAME = f"gateway-{_MAX_PID_TEXT}-{_MAX_PROFILE_HASH}.json"
_GENERIC_TEMP_NAME = f".{_GENERIC_DESCRIPTOR_NAME}.{_MAX_INSTANCE_UUID}.tmp"
_RELAY_DESCRIPTOR_NAME = f"gateway-{_MAX_PID_TEXT}-{_MAX_INSTANCE_UUID}.json"
_RELAY_TEMP_NAME = f".{_RELAY_DESCRIPTOR_NAME}.{_MAX_INSTANCE_UUID}.tmp"
_HAS_DESCRIPTOR_RELATIVE_DIRECTORIES = all(
    function in os.supports_dir_fd
    for function in (os.open, os.mkdir, os.rename, os.rmdir, os.stat)
) and all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW"))


def _canonical_directory(directory: Path) -> Path:
    raw = Path(directory).expanduser()
    if "\x00" in str(raw):
        raise ValueError("Local Gateway paths must not contain NUL")
    if ".." in raw.parts:
        raise ValueError("Local Gateway paths must not contain parent traversal")
    if not raw.is_absolute():
        raise ValueError("Local Gateway paths must be absolute")
    _validate_component_lengths(raw)
    return raw


def _fits(path: Path, child_name: str, maximum: int) -> bool:
    return len(os.fsencode(path / child_name)) <= maximum


def _name_max(path: Path) -> int:
    try:
        limit = os.pathconf(path, "PC_NAME_MAX")
    except (OSError, ValueError) as error:
        raise ValueError("Local Gateway NAME_MAX cannot be determined") from error
    if type(limit) is not int or limit <= 0:
        raise ValueError("Local Gateway NAME_MAX is invalid")
    return limit


def _validate_component_lengths(directory: Path, *child_names: str) -> None:
    existing_parent = Path(directory.anchor)
    name_max = _name_max(existing_parent)
    reached_missing_component = False
    for component in (part for part in directory.parts if part != directory.anchor):
        if len(os.fsencode(component)) > name_max:
            raise ValueError("Local Gateway path component exceeds NAME_MAX")
        if reached_missing_component:
            continue
        candidate = existing_parent / component
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            reached_missing_component = True
        except OSError as error:
            raise ValueError("Local Gateway path cannot be inspected") from error
        else:
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("Local Gateway paths must not contain symlinks")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Local Gateway path components must be directories")
            existing_parent = candidate
            name_max = _name_max(existing_parent)
    if any(len(os.fsencode(child_name)) > name_max for child_name in child_names):
        raise ValueError("Local Gateway path component exceeds NAME_MAX")


@dataclass(frozen=True, slots=True)
class MacOSLocalGatewayPaths:
    """Role-separated registry and socket directories resolved as one unit."""

    local_gateway_registry_directory: Path
    local_gateway_socket_directory: Path
    control_registry_directory: Path
    control_socket_directory: Path
    observer_registry_directory: Path
    observer_socket_directory: Path

    def __post_init__(self) -> None:
        for field_name in (
            "local_gateway_registry_directory",
            "local_gateway_socket_directory",
            "control_registry_directory",
            "control_socket_directory",
            "observer_registry_directory",
            "observer_socket_directory",
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_directory(getattr(self, field_name)),
            )
        if (
            any(
                not _fits(
                    directory,
                    max(
                        (_GENERIC_TEMP_NAME, _RELAY_TEMP_NAME),
                        key=lambda value: len(os.fsencode(value)),
                    ),
                    _MAX_FILESYSTEM_PATH_BYTES,
                )
                for directory in self.registry_directories
            )
            or not _fits(
                self.local_gateway_socket_directory,
                _GENERIC_SOCKET_NAME,
                _MAX_UDS_PATH_BYTES,
            )
            or not _fits(
                self.control_socket_directory,
                _CONTROL_SOCKET_NAME,
                _MAX_UDS_PATH_BYTES,
            )
            or not _fits(
                self.observer_socket_directory,
                _OBSERVER_SOCKET_NAME,
                _MAX_UDS_PATH_BYTES,
            )
        ):
            raise ValueError("Local Gateway final path exceeds safe length")
        for directory in self.registry_directories:
            _validate_component_lengths(
                directory,
                _GENERIC_DESCRIPTOR_NAME,
                _GENERIC_TEMP_NAME,
                _RELAY_DESCRIPTOR_NAME,
                _RELAY_TEMP_NAME,
            )
        _validate_component_lengths(
            self.local_gateway_socket_directory,
            _GENERIC_SOCKET_NAME,
        )
        _validate_component_lengths(
            self.control_socket_directory,
            _CONTROL_SOCKET_NAME,
        )
        _validate_component_lengths(
            self.observer_socket_directory,
            _OBSERVER_SOCKET_NAME,
        )
        if len(set(self.registry_directories)) != len(self.registry_directories):
            raise ValueError("Local Gateway registry directories must be distinct")
        if len(set(self.socket_directories)) != len(self.socket_directories):
            raise ValueError("Local Gateway socket directories must be distinct")
        if len({*self.registry_directories, *self.socket_directories}) != len(
            self.registry_directories
        ) + len(self.socket_directories):
            raise ValueError("Local Gateway all six directories must be distinct")

    @property
    def registry_directories(self) -> tuple[Path, Path, Path]:
        return (
            self.local_gateway_registry_directory,
            self.control_registry_directory,
            self.observer_registry_directory,
        )

    @property
    def socket_directories(self) -> tuple[Path, Path, Path]:
        return (
            self.local_gateway_socket_directory,
            self.control_socket_directory,
            self.observer_socket_directory,
        )


def load_local_gateway_paths(
    environment: Mapping[str, str] | None = None,
    *,
    hermes_home: Path | None = None,
    temporary_directory: Path | None = None,
    effective_uid: int | None = None,
) -> MacOSLocalGatewayPaths:
    """Resolve all role paths together without creating filesystem resources."""

    source = os.environ if environment is None else environment
    default_home = Path.home().resolve(strict=True) / ".hermes"
    resolved_home = Path(
        hermes_home
        if hermes_home is not None
        else source.get("HERMES_HOME", default_home)
    ).expanduser()
    if resolved_home.parent.name == "profiles":
        resolved_home = resolved_home.parent.parent
    system_temporary_directory = Path("/tmp").resolve(strict=True)
    resolved_temporary_directory = Path(
        temporary_directory
        if temporary_directory is not None
        else system_temporary_directory
    ).expanduser()
    uid = (
        os.getuid()
        if effective_uid is None and hasattr(os, "getuid")
        else os.getpid()
        if effective_uid is None
        else effective_uid
    )

    def configured(name: str, default: Path) -> Path:
        if name not in source:
            return default
        value = source[name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must not be empty")
        return Path(value).expanduser()

    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=configured(
            "HERMES_LOCAL_GATEWAY_REGISTRY_DIR",
            resolved_home / "runtime" / "local-gateways",
        ),
        local_gateway_socket_directory=configured(
            "HERMES_LOCAL_GATEWAY_SOCKET_DIR",
            resolved_temporary_directory / f"hermes-local-gateway-{uid}",
        ),
        control_registry_directory=configured(
            "HERMES_CONTROL_REGISTRY_DIR",
            resolved_home / "runtime" / "control-gateways",
        ),
        control_socket_directory=configured(
            "HERMES_CONTROL_SOCKET_DIR",
            system_temporary_directory / f"hermes-control-{uid}",
        ),
        observer_registry_directory=configured(
            "HERMES_OBSERVER_REGISTRY_DIR",
            resolved_home / "runtime" / "observer-gateways",
        ),
        observer_socket_directory=configured(
            "HERMES_OBSERVER_SOCKET_DIR",
            system_temporary_directory / f"hermes-observer-{uid}",
        ),
    )


def _directory_identity(directory: Path) -> tuple[int, int]:
    metadata = directory.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _preflight_directories(directories: tuple[Path, ...]) -> None:
    for directory in directories:
        _validate_component_lengths(directory)
    casefolded = tuple(str(directory).casefold() for directory in directories)
    if len(set(casefolded)) != len(casefolded):
        raise ValueError(
            "Local Gateway directories must not be case-insensitive aliases"
        )

    existing_identities: list[tuple[int, int]] = []
    for directory in directories:
        try:
            directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError("Local Gateway path cannot be inspected") from error
        if not is_private_directory(directory):
            raise ValueError("untrusted local directory")
        existing_identities.append(_directory_identity(directory))
    if len(set(existing_identities)) != len(existing_identities):
        raise ValueError("Local Gateway physical directories must be distinct")


@dataclass(slots=True)
class _OwnedDirectory:
    path: Path
    parent_descriptor: int
    leaf: str
    device: int
    inode: int


def _require_descriptor_relative_directories() -> None:
    if not _HAS_DESCRIPTOR_RELATIVE_DIRECTORIES:
        raise RuntimeError(
            "descriptor-relative Local Gateway directory operations are unavailable"
        )


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _descriptor_relative_identity(directory: Path) -> tuple[int, int]:
    flags = _directory_open_flags()
    descriptor = os.open(directory.anchor, flags)
    try:
        for component in (part for part in directory.parts if part != directory.anchor):
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child_descriptor
                child_descriptor = None
            finally:
                if child_descriptor is not None:
                    os.close(child_descriptor)
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino
    except OSError as error:
        raise ValueError("Local Gateway directory changed during creation") from error
    finally:
        os.close(descriptor)


def _is_private_directory_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )


def _create_private_directory(
    directory: Path,
    created: list[_OwnedDirectory],
) -> tuple[Path, tuple[int, int]]:
    flags = _directory_open_flags()
    descriptor = os.open(directory.anchor, flags)
    try:
        components = tuple(part for part in directory.parts if part != directory.anchor)
        for component_index, component in enumerate(components):
            component_path = Path(directory.anchor, *components[: component_index + 1])
            was_created = False
            ownership_recorded = False
            child_descriptor: int | None = None
            metadata: os.stat_result | None = None
            try:
                try:
                    child_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        was_created = True
                    except FileExistsError:
                        pass
                    except OSError as error:
                        raise ValueError("untrusted local directory") from error
                    try:
                        child_descriptor = os.open(
                            component,
                            flags,
                            dir_fd=descriptor,
                        )
                    except OSError as error:
                        raise ValueError("untrusted local directory") from error
                except OSError as error:
                    raise ValueError("untrusted local directory") from error

                metadata = os.fstat(child_descriptor)
                if was_created:
                    parent_descriptor = os.dup(descriptor)
                    try:
                        created.append(
                            _OwnedDirectory(
                                path=component_path,
                                parent_descriptor=parent_descriptor,
                                leaf=component,
                                device=metadata.st_dev,
                                inode=metadata.st_ino,
                            )
                        )
                    except BaseException:
                        os.close(parent_descriptor)
                        raise
                    ownership_recorded = True
                os.close(descriptor)
                descriptor = child_descriptor
                child_descriptor = None
            except BaseException:
                if (
                    was_created
                    and not ownership_recorded
                    and child_descriptor is not None
                ):
                    if metadata is None:
                        try:
                            metadata = os.fstat(child_descriptor)
                        except OSError:
                            metadata = None
                    if metadata is not None:
                        _rollback_owned_directory(
                            _OwnedDirectory(
                                path=component_path,
                                parent_descriptor=descriptor,
                                leaf=component,
                                device=metadata.st_dev,
                                inode=metadata.st_ino,
                            )
                        )
                raise
            finally:
                if child_descriptor is not None:
                    os.close(child_descriptor)

        metadata = os.fstat(descriptor)
        if not _is_private_directory_metadata(metadata):
            raise ValueError("untrusted local directory")
        return directory, (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)


def _restore_replacement(
    owned: _OwnedDirectory,
    quarantine: str,
) -> None:
    try:
        os.stat(
            owned.leaf,
            dir_fd=owned.parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        try:
            os.rename(
                quarantine,
                owned.leaf,
                src_dir_fd=owned.parent_descriptor,
                dst_dir_fd=owned.parent_descriptor,
            )
        except OSError:
            pass
    except OSError:
        pass


def _rollback_owned_directory(owned: _OwnedDirectory) -> None:
    quarantine = f".hermes-rollback-{secrets.token_hex(12)}"
    try:
        metadata = os.stat(
            owned.leaf,
            dir_fd=owned.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != owned.device
            or metadata.st_ino != owned.inode
        ):
            return
        os.rename(
            owned.leaf,
            quarantine,
            src_dir_fd=owned.parent_descriptor,
            dst_dir_fd=owned.parent_descriptor,
        )
        quarantined = os.stat(
            quarantine,
            dir_fd=owned.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(quarantined.st_mode)
            or quarantined.st_dev != owned.device
            or quarantined.st_ino != owned.inode
        ):
            _restore_replacement(owned, quarantine)
            return
        try:
            os.rmdir(quarantine, dir_fd=owned.parent_descriptor)
        except OSError:
            try:
                os.rename(
                    quarantine,
                    owned.leaf,
                    src_dir_fd=owned.parent_descriptor,
                    dst_dir_fd=owned.parent_descriptor,
                )
            except OSError:
                pass
    except OSError:
        return


def _rollback_created_directories(
    created: list[_OwnedDirectory],
) -> None:
    for owned in reversed(created):
        try:
            _rollback_owned_directory(owned)
        finally:
            os.close(owned.parent_descriptor)


def _close_ownership_records(created: list[_OwnedDirectory]) -> None:
    for owned in created:
        os.close(owned.parent_descriptor)


@contextmanager
def provision_distinct_local_gateway_directories(
    paths: MacOSLocalGatewayPaths,
) -> Iterator[MacOSLocalGatewayPaths]:
    """Provision six private directories, rolling back owned empties on failure."""

    directories = (*paths.registry_directories, *paths.socket_directories)
    _require_descriptor_relative_directories()
    _preflight_directories(directories)
    created: list[_OwnedDirectory] = []
    try:
        secured = tuple(
            _create_private_directory(directory, created) for directory in directories
        )
        ensured = tuple(directory for directory, _identity in secured)
        if ensured != directories:
            raise ValueError("Local Gateway directory changed during creation")
        identities = tuple(identity for _directory, identity in secured)
        pathname_identities = tuple(
            _directory_identity(directory) for directory in ensured
        )
        if len(set(identities)) != len(identities):
            raise ValueError("Local Gateway physical directories must be distinct")
        if len(set(pathname_identities)) != len(pathname_identities):
            raise ValueError("Local Gateway physical directories must be distinct")
        trusted_identities = tuple(
            _descriptor_relative_identity(directory) for directory in ensured
        )
        if pathname_identities != identities or trusted_identities != identities:
            raise ValueError("Local Gateway directory changed during creation")
        yield paths
    except BaseException:
        _rollback_created_directories(created)
        raise
    else:
        _close_ownership_records(created)


def ensure_distinct_local_gateway_directories(
    paths: MacOSLocalGatewayPaths,
) -> MacOSLocalGatewayPaths:
    """Create the six private directories and reject physical aliases."""

    with provision_distinct_local_gateway_directories(paths):
        pass
    return paths


__all__ = [
    "MacOSLocalGatewayPaths",
    "ensure_distinct_local_gateway_directories",
    "load_local_gateway_paths",
    "provision_distinct_local_gateway_directories",
]
