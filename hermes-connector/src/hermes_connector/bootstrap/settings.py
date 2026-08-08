"""Safe production runtime settings for Hermes Connector."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_MAX_CONFIG_BYTES = 65_536


def _pathconf_limit(name: str, fallback: int) -> int:
    pathconf = getattr(os, "pathconf", None)
    if pathconf is None:
        return fallback
    try:
        value = int(pathconf("/", name))
    except (OSError, TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


_MAX_PATH_BYTES = _pathconf_limit("PC_PATH_MAX", 32_767)
_MAX_NAME_BYTES = _pathconf_limit("PC_NAME_MAX", 255)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SEMVER = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_SUPPORTED_PLATFORMS = frozenset({"macos", "windows"})
_SECRET_ENVIRONMENT_KEYS = frozenset(
    {
        "HERMES_CONNECTOR_ACCESS_TOKEN",
        "HERMES_CONNECTOR_TOKEN",
        "HERMES_ACCESS_TOKEN",
    }
)
_FORBIDDEN_AUTHORITY_ENVIRONMENT_KEYS = frozenset(
    {
        "HERMES_CONNECTOR_TENANT_ID",
        "HERMES_CONNECTOR_DEVICE_ID",
        "HERMES_CONNECTOR_AGENT_ID",
        "HERMES_CONNECTOR_CREDENTIAL_ID",
        "HERMES_CONNECTOR_SCOPES",
        "HERMES_CONNECTOR_RUNTIME_GENERATION",
    }
)
_FORBIDDEN_LOCAL_RUNTIME_CONFIG_FIELDS = frozenset({"runtime_generation"})
_FIELD_ENVIRONMENT_KEYS = {
    "cloud_endpoint": "HERMES_CONNECTOR_CLOUD_ENDPOINT",
    "cloud_api_endpoint": "HERMES_CONNECTOR_API_ENDPOINT",
    "display_name": "HERMES_CONNECTOR_DISPLAY_NAME",
    "profile": "HERMES_CONNECTOR_PROFILE",
    "connector_version": "HERMES_CONNECTOR_VERSION",
    "local_gateway_registry_directory": ("HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR"),
    "local_gateway_socket_directory": ("HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR"),
    "control_registry_directory": "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
    "control_socket_directory": "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
    "observer_registry_directory": "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
    "observer_socket_directory": "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    "state_directory": "HERMES_CONNECTOR_STATE_DIR",
    "database_file": "HERMES_CONNECTOR_DATABASE_FILE",
    "lock_file": "HERMES_CONNECTOR_LOCK_FILE",
    "credential_store": "HERMES_CONNECTOR_CREDENTIAL_STORE",
    "token_file": "HERMES_CONNECTOR_TOKEN_FILE",
}
_LEGACY_GATEWAY_ENVIRONMENT_KEYS = frozenset(
    {"HERMES_CONNECTOR_REGISTRY_DIR", "HERMES_CONNECTOR_SOCKET_DIR"}
)
_LEGACY_GATEWAY_CONFIG_FIELDS = frozenset({"registry_directory", "socket_directory"})
_CONFIG_FIELDS = frozenset(_FIELD_ENVIRONMENT_KEYS)
_OPTIONAL_CONFIG_FIELDS = frozenset(
    {
        "credential_store",
        "token_file",
        "display_name",
    }
)
_REQUIRED_CONFIG_FIELDS = _CONFIG_FIELDS - _OPTIONAL_CONFIG_FIELDS
_GATEWAY_FIELDS = (
    "local_gateway_registry_directory",
    "local_gateway_socket_directory",
    "control_registry_directory",
    "control_socket_directory",
    "observer_registry_directory",
    "observer_socket_directory",
)
_WINDOWS_DEFAULTED_FIELDS = (*_GATEWAY_FIELDS, "state_directory", "database_file", "lock_file")


class RuntimeConfigurationError(ValueError):
    """Runtime configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ConnectorRuntimeSettings:
    cloud_endpoint: str
    cloud_api_endpoint: str
    display_name: str
    profile: str
    connector_version: str
    local_gateway_registry_directory: Path
    local_gateway_socket_directory: Path
    control_registry_directory: Path
    control_socket_directory: Path
    observer_registry_directory: Path
    observer_socket_directory: Path
    state_directory: Path
    database_file: Path
    lock_file: Path
    credential_store: str
    token_file: Path | None

    @property
    def instance_state_file(self) -> Path:
        return self.state_directory / "instances.json"

    @property
    def paired_projection_file(self) -> Path:
        return self.state_directory / "paired.json"

    @property
    def pairing_offer_projection_file(self) -> Path:
        return self.state_directory / "pairing-offer.json"

    @property
    def pairing_command_lock_file(self) -> Path:
        return self.state_directory / "pairing-command.lock"

    @property
    def status_receipt_file(self) -> Path:
        return self.state_directory / "status.json"


def load_runtime_settings(
    environment: Mapping[str, str] | None = None,
    *,
    platform_name: str = "macos",
) -> ConnectorRuntimeSettings:
    if platform_name not in _SUPPORTED_PLATFORMS:
        raise RuntimeConfigurationError("runtime platform is unsupported")
    source = dict(os.environ if environment is None else environment)
    if any(key in source for key in _SECRET_ENVIRONMENT_KEYS):
        raise RuntimeConfigurationError("plaintext token configuration is forbidden")
    if any(key in source for key in _FORBIDDEN_AUTHORITY_ENVIRONMENT_KEYS):
        if "HERMES_CONNECTOR_RUNTIME_GENERATION" in source:
            raise RuntimeConfigurationError(
                "local runtime authority configuration is forbidden"
            )
        raise RuntimeConfigurationError(
            "local authorization identity configuration is forbidden"
        )
    if any(key in source for key in _LEGACY_GATEWAY_ENVIRONMENT_KEYS):
        raise RuntimeConfigurationError("legacy generic gateway paths are forbidden")

    configured: dict[str, object] = {}
    config_reference = source.get("HERMES_CONNECTOR_CONFIG_FILE")
    if config_reference is not None:
        configured.update(
            _read_config_file(Path(config_reference), platform_name=platform_name)
        )
    for field, key in _FIELD_ENVIRONMENT_KEYS.items():
        if key in source:
            configured[field] = source[key]

    default_fields = _WINDOWS_DEFAULTED_FIELDS if platform_name == "windows" else _GATEWAY_FIELDS
    if any(field not in configured for field in default_fields):
        hermes_home = _resolve_hermes_home(source)
        if platform_name == "windows":
            _apply_windows_defaults(configured, hermes_home)
        else:
            _apply_macos_gateway_defaults(configured, hermes_home)

    configured_fields = frozenset(configured)
    if (
        not _REQUIRED_CONFIG_FIELDS <= configured_fields
        or not configured_fields <= _CONFIG_FIELDS
    ):
        raise RuntimeConfigurationError("required runtime configuration is incomplete")
    if any(not isinstance(value, str) for value in configured.values()):
        raise RuntimeConfigurationError("runtime configuration values must be strings")

    cloud_endpoint = str(configured["cloud_endpoint"])
    _validate_endpoint(cloud_endpoint)
    cloud_api_endpoint = str(configured["cloud_api_endpoint"])
    _validate_api_endpoint(cloud_api_endpoint)
    profile = _identifier(configured["profile"], "profile")
    display_name = str(configured.get("display_name", "Hermes Connector"))
    if (
        not 1 <= len(display_name) <= 128
        or display_name != display_name.strip()
        or any(ord(character) < 32 for character in display_name)
    ):
        raise RuntimeConfigurationError("connector display name is invalid")
    connector_version = str(configured["connector_version"])
    if _SEMVER.fullmatch(connector_version) is None:
        raise RuntimeConfigurationError("connector version is invalid")
    paths: dict[str, Path] = {
        field: _absolute_path(configured[field], field)
        for field in (
            "local_gateway_registry_directory",
            "local_gateway_socket_directory",
            "control_registry_directory",
            "control_socket_directory",
            "observer_registry_directory",
            "observer_socket_directory",
            "state_directory",
            "database_file",
            "lock_file",
        )
    }
    role_paths = {paths[field] for field in _GATEWAY_FIELDS}
    if len(role_paths) != len(_GATEWAY_FIELDS):
        raise RuntimeConfigurationError("gateway role paths must be distinct")
    role_identities = {
        identity
        for path in role_paths
        if (identity := _physical_identity(path)) is not None
    }
    if len(role_identities) != sum(
        _physical_identity(path) is not None for path in role_paths
    ):
        raise RuntimeConfigurationError(
            "gateway role paths must be physically distinct"
        )

    credential_store, token_file = _credential_configuration(
        configured,
        platform_name=platform_name,
    )

    managed_files = {
        paths["database_file"],
        paths["lock_file"],
        paths["state_directory"] / "instances.json",
        paths["state_directory"] / "paired.json",
        paths["state_directory"] / "pairing-offer.json",
        paths["state_directory"] / "pairing-command.lock",
    }
    if token_file is not None:
        managed_files.add(token_file)
    expected_managed_files = 7 if token_file is not None else 6
    if len(managed_files) != expected_managed_files:
        raise RuntimeConfigurationError("managed runtime files must be distinct")
    for path in managed_files:
        _validate_path_budget(path, "managed runtime child")
    return ConnectorRuntimeSettings(
        cloud_endpoint=cloud_endpoint,
        cloud_api_endpoint=cloud_api_endpoint,
        display_name=display_name,
        profile=profile,
        connector_version=connector_version,
        local_gateway_registry_directory=paths["local_gateway_registry_directory"],
        local_gateway_socket_directory=paths["local_gateway_socket_directory"],
        control_registry_directory=paths["control_registry_directory"],
        control_socket_directory=paths["control_socket_directory"],
        observer_registry_directory=paths["observer_registry_directory"],
        observer_socket_directory=paths["observer_socket_directory"],
        state_directory=paths["state_directory"],
        database_file=paths["database_file"],
        lock_file=paths["lock_file"],
        credential_store=credential_store,
        token_file=token_file,
    )


def _resolve_hermes_home(source: Mapping[str, str]) -> Path:
    raw_hermes_home = source.get("HERMES_HOME")
    if raw_hermes_home is None:
        try:
            raw_hermes_home = os.fspath(Path.home().resolve(strict=True) / ".hermes")
        except (OSError, RuntimeError):
            raise RuntimeConfigurationError("HERMES_HOME must be canonical") from None
    if not isinstance(raw_hermes_home, str):
        raise RuntimeConfigurationError("HERMES_HOME must be a path")
    if raw_hermes_home.startswith("~") or not Path(raw_hermes_home).is_absolute():
        raise RuntimeConfigurationError("HERMES_HOME must be an absolute path")
    hermes_home = _absolute_path(raw_hermes_home, "HERMES_HOME")
    if hermes_home.parent.name == "profiles":
        hermes_home = hermes_home.parent.parent
    return hermes_home


def _apply_macos_gateway_defaults(
    configured: dict[str, object],
    hermes_home: Path,
) -> None:
    temporary_root = Path("/tmp").resolve(strict=True)
    uid = os.geteuid()
    defaults = {
        "local_gateway_registry_directory": hermes_home / "runtime" / "local-gateways",
        "local_gateway_socket_directory": temporary_root / f"hermes-local-gateway-{uid}",
        "control_registry_directory": hermes_home / "runtime" / "control-gateways",
        "control_socket_directory": temporary_root / f"hermes-control-{uid}",
        "observer_registry_directory": hermes_home / "runtime" / "observer-gateways",
        "observer_socket_directory": temporary_root / f"hermes-observer-{uid}",
    }
    for field, path in defaults.items():
        configured.setdefault(field, os.fspath(path))


def _apply_windows_defaults(
    configured: dict[str, object],
    hermes_home: Path,
) -> None:
    profile = _identifier(configured.get("profile"), "profile")
    runtime_root = hermes_home / "runtime" / "windows"
    profile_root = hermes_home / "connector" / "profiles" / profile
    state_root = profile_root / "state"
    defaults = {
        "local_gateway_registry_directory": runtime_root / "local-gateway-registry",
        "local_gateway_socket_directory": runtime_root / "local-gateway-pipe",
        "control_registry_directory": runtime_root / "control-registry",
        "control_socket_directory": runtime_root / "control-pipe",
        "observer_registry_directory": runtime_root / "observer-registry",
        "observer_socket_directory": runtime_root / "observer-pipe",
        "state_directory": state_root,
        "database_file": state_root / "connector.sqlite3",
        "lock_file": state_root / "connector.lock",
    }
    for field, path in defaults.items():
        configured.setdefault(field, os.fspath(path))


def _credential_configuration(
    configured: Mapping[str, object],
    *,
    platform_name: str,
) -> tuple[str, Path | None]:
    default_store = "dpapi" if platform_name == "windows" else "keychain"
    credential_store = str(configured.get("credential_store", default_store))
    token_reference = configured.get("token_file")
    if platform_name == "windows":
        if credential_store != "dpapi":
            raise RuntimeConfigurationError("Windows credential store must be dpapi")
        if token_reference is not None:
            raise RuntimeConfigurationError(
                "formal DPAPI credentials cannot self-assert authorization"
            )
        return credential_store, None

    if credential_store not in {"keychain", "file"}:
        raise RuntimeConfigurationError("credential store is invalid")
    if credential_store == "keychain":
        if token_reference is not None:
            raise RuntimeConfigurationError(
                "formal keychain credentials cannot self-assert authorization"
            )
        return credential_store, None
    if token_reference is None:
        raise RuntimeConfigurationError("file credential migration mode is incomplete")
    return credential_store, _absolute_path(token_reference, "token_file")


def _read_config_file(
    path: Path,
    *,
    platform_name: str,
) -> dict[str, object]:
    if not path.is_absolute() or "\x00" in str(path):
        raise RuntimeConfigurationError("configuration file reference is unsafe")
    if platform_name == "windows":
        raw = _read_windows_config_file(path)
    else:
        raw = _read_macos_config_file(path)
    return _decode_config(raw)


def _read_windows_config_file(path: Path) -> bytes:
    from hermes_connector.adapters.platform.windows.private_state import (
        read_private_file,
    )

    try:
        raw = read_private_file(path, maximum=_MAX_CONFIG_BYTES)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RuntimeConfigurationError("configuration file is unavailable") from None
    if not 1 <= len(raw) <= _MAX_CONFIG_BYTES:
        raise RuntimeConfigurationError("configuration file metadata is unsafe")
    return raw


def _read_macos_config_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        raise RuntimeConfigurationError("configuration file is unavailable") from None
    _validate_macos_config_metadata(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RuntimeConfigurationError("configuration file is unavailable") from None
    try:
        opened = os.fstat(descriptor)
        _validate_macos_config_metadata(opened)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise RuntimeConfigurationError("configuration file changed during read")
        raw = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) != opened.st_size or len(raw) > _MAX_CONFIG_BYTES:
        raise RuntimeConfigurationError("configuration file changed during read")
    return raw


def _decode_config(raw: bytes) -> dict[str, object]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError):
        raise RuntimeConfigurationError(
            "configuration file content is invalid"
        ) from None
    if isinstance(value, dict) and frozenset(value).intersection(
        _LEGACY_GATEWAY_CONFIG_FIELDS
    ):
        raise RuntimeConfigurationError("legacy generic gateway paths are forbidden")
    if isinstance(value, dict) and frozenset(value).intersection(
        _FORBIDDEN_LOCAL_RUNTIME_CONFIG_FIELDS
    ):
        raise RuntimeConfigurationError(
            "local runtime authority configuration is forbidden"
        )
    if not isinstance(value, dict) or not frozenset(value) <= _CONFIG_FIELDS:
        raise RuntimeConfigurationError("configuration file fields are invalid")
    return value


def _validate_macos_config_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & ~0o600
        or not 1 <= metadata.st_size <= _MAX_CONFIG_BYTES
    ):
        raise RuntimeConfigurationError("configuration file metadata is unsafe")


def _validate_endpoint(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RuntimeConfigurationError("cloud endpoint is invalid") from None
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise RuntimeConfigurationError("cloud endpoint is invalid")


def _validate_api_endpoint(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RuntimeConfigurationError("cloud API endpoint is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise RuntimeConfigurationError("cloud API endpoint is invalid")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RuntimeConfigurationError(f"{name} is invalid")
    return value


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str):
        raise RuntimeConfigurationError(f"{name} must be a path")
    path = Path(value)
    if not path.is_absolute() or "\x00" in value:
        raise RuntimeConfigurationError(f"{name} must be an absolute path")
    if ".." in path.parts or "." in path.parts:
        raise RuntimeConfigurationError(f"{name} must be canonical")
    _validate_path_budget(path, name)
    if _has_symlink_component(path):
        raise RuntimeConfigurationError(f"{name} must be canonical and nonsymlinked")
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise RuntimeConfigurationError(f"{name} must be canonical") from None
    _validate_path_budget(canonical, name)
    return canonical


def _validate_path_budget(path: Path, name: str) -> None:
    try:
        encoded = os.fsencode(path)
        components = tuple(
            os.fsencode(component)
            for component in path.parts
            if component not in {path.anchor, os.sep}
        )
    except (TypeError, UnicodeEncodeError):
        raise RuntimeConfigurationError(f"{name} exceeds the path budget") from None
    if len(encoded) > _MAX_PATH_BYTES or any(
        len(component) > _MAX_NAME_BYTES for component in components
    ):
        raise RuntimeConfigurationError(f"{name} exceeds the path budget")


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(metadata.st_mode):
            return True
    return False


def _physical_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino
