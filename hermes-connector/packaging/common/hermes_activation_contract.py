"""Auditable contracts shared by local release activation tooling."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ActivationContractError(ValueError):
    """Activation input or release evidence is not trustworthy."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""


@dataclass(frozen=True)
class SystemCommand:
    purpose: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class HealthGate:
    name: str
    argv: tuple[str, ...]
    expected_fields: Mapping[str, Any]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    process_start_time_ns: int
    process_executable: Path
    process_executable_device: int
    process_executable_inode: int
    profile: str
    hermes_home: Path
    authority_id: str
    runtime_generation: int
    instance_id: str
    host_bundle_id: str

    def to_json(self) -> Mapping[str, Any]:
        return {
            "pid": self.pid,
            "process_start_time_ns": self.process_start_time_ns,
            "process_executable": str(self.process_executable),
            "process_executable_device": self.process_executable_device,
            "process_executable_inode": self.process_executable_inode,
            "profile": self.profile,
            "hermes_home": str(self.hermes_home),
            "authority_id": self.authority_id,
            "runtime_generation": self.runtime_generation,
            "instance_id": self.instance_id,
            "host_bundle_id": self.host_bundle_id,
        }

    @classmethod
    def from_json(cls, value: Any) -> ProcessIdentity:
        fields = {
            "pid",
            "process_start_time_ns",
            "process_executable",
            "process_executable_device",
            "process_executable_inode",
            "profile",
            "hermes_home",
            "authority_id",
            "runtime_generation",
            "instance_id",
            "host_bundle_id",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ActivationContractError("old authority receipt has invalid fields")
        try:
            result = cls(
                pid=int(value["pid"]),
                process_start_time_ns=int(value["process_start_time_ns"]),
                process_executable=Path(value["process_executable"]),
                process_executable_device=int(value["process_executable_device"]),
                process_executable_inode=int(value["process_executable_inode"]),
                profile=str(value["profile"]),
                hermes_home=Path(value["hermes_home"]),
                authority_id=str(value["authority_id"]),
                runtime_generation=int(value["runtime_generation"]),
                instance_id=str(value["instance_id"]),
                host_bundle_id=str(value["host_bundle_id"]),
            )
        except (TypeError, ValueError) as exc:
            raise ActivationContractError("old authority receipt is invalid") from exc
        if (
            result.pid <= 0
            or result.process_start_time_ns <= 0
            or result.process_executable_device < 0
            or result.process_executable_inode <= 0
            or not result.authority_id
            or result.runtime_generation <= 0
            or not result.instance_id
            or not result.host_bundle_id
            or not result.profile
            or not result.process_executable.is_absolute()
            or not result.hermes_home.is_absolute()
        ):
            raise ActivationContractError("old authority receipt is invalid")
        return result


@dataclass(frozen=True)
class RoleDescriptorEvidence:
    role: str
    pid: int
    process_start_time_ns: int
    process_executable: Path
    process_executable_device: int
    process_executable_inode: int
    authority_id: str
    runtime_generation: int
    instance_id: str
    host_bundle_id: str
    socket_path: Path
    socket_device: int
    socket_inode: int
    is_socket: bool
    peer_pid: int


@dataclass(frozen=True)
class HostRuntimeEvidence:
    process: ProcessIdentity
    plugin_store_active: bool
    plugin_manifest_path: Path
    trust_store_path: Path
    descriptors: tuple[RoleDescriptorEvidence, ...]


@dataclass(frozen=True)
class ValidatedRelease:
    release_dir: Path
    release_id: str
    release_digest: str
    manifest: Mapping[str, Any]
    host_plist: Path
    connector_plist: Path


def validate_b1_release(
    release_dir: Path,
    *,
    expected_store_root: Path,
) -> ValidatedRelease:
    release_dir = _canonical_directory(release_dir, "release")
    manifest_path = release_dir / "manifest" / "release.json"
    manifest = _private_json_file(manifest_path, mode=0o600)
    required = {
        "schema_version",
        "release_id",
        "core",
        "connector",
        "plugin",
        "signed_plugin_manifest",
        "services",
        "release_digest",
        "verification",
        "receipts",
        "build_policy",
    }
    if set(manifest) != required or manifest.get("schema_version") != 1:
        raise ActivationContractError("B1 release manifest has invalid schema")
    if manifest["release_id"] != release_dir.name:
        raise ActivationContractError("B1 release_id does not match directory")
    payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "release_id",
            "core",
            "connector",
            "plugin",
            "signed_plugin_manifest",
            "services",
        )
    }
    digest = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    if manifest["release_digest"] != digest:
        raise ActivationContractError("B1 release manifest digest mismatch")

    signed = manifest["signed_plugin_manifest"]
    if not isinstance(signed, dict):
        raise ActivationContractError("signed Plugin manifest is invalid")
    if signed.get("store_root") != str(expected_store_root.resolve(strict=False)):
        raise ActivationContractError(
            "signed Plugin store_root does not match profile state"
        )
    if _is_within(Path(signed.get("store_root", "")), release_dir):
        raise ActivationContractError(
            "mutable Plugin store_root is inside immutable release"
        )

    artifacts = [
        (
            _release_file(
                release_dir,
                Path("receipts/inputs/core") / manifest["core"]["wheel_path"],
            ),
            manifest["core"]["wheel_sha256"],
            0o400,
        ),
        (
            release_dir / "host" / "project" / "pyproject.toml",
            manifest["core"]["project_sha256"],
            0o400,
        ),
        (
            release_dir / "host" / "project" / "uv.lock",
            manifest["core"]["lock_sha256"],
            0o400,
        ),
        (
            _release_file(
                release_dir,
                Path("receipts/inputs/connector") / manifest["connector"]["wheel_path"],
            ),
            manifest["connector"]["wheel_sha256"],
            0o400,
        ),
        (
            release_dir / "connector" / "project" / "pyproject.toml",
            manifest["connector"]["project_sha256"],
            0o400,
        ),
        (
            release_dir / "connector" / "project" / "uv.lock",
            manifest["connector"]["lock_sha256"],
            0o400,
        ),
        (
            _release_file(release_dir, manifest["plugin"]["bundle_path"]),
            manifest["plugin"]["bundle_sha256"],
            0o400,
        ),
        (
            _release_file(release_dir, manifest["plugin"]["trust_store_path"]),
            manifest["plugin"]["store_manifest_sha256"],
            0o400,
        ),
    ]
    for path, expected_digest, expected_mode in artifacts:
        _digest_file(path, expected_digest, expected_mode)
    signed_path = _release_file(release_dir, manifest["plugin"]["signed_manifest_path"])
    if _private_json_file(signed_path, mode=0o400) != signed:
        raise ActivationContractError("signed Plugin manifest copy mismatch")
    if Path(signed["wheel_path"]) != _release_file(
        release_dir, manifest["plugin"]["bundle_path"]
    ):
        raise ActivationContractError(
            "signed Plugin wheel_path does not match immutable artifact"
        )

    services = manifest["services"]
    if not isinstance(services, dict) or set(services) != {
        "com.hermes.host.plist",
        "com.hermes.connector.plist",
    }:
        raise ActivationContractError("B1 release is missing macOS service templates")
    for name, declaration in services.items():
        if not isinstance(declaration, dict) or set(declaration) != {"sha256"}:
            raise ActivationContractError("B1 service digest declaration is invalid")
        _digest_file(release_dir / "services" / name, declaration["sha256"], 0o600)
    for immutable_root in (
        release_dir / "plugin",
        release_dir / "receipts" / "inputs",
        release_dir / "host" / "project",
        release_dir / "connector" / "project",
    ):
        if immutable_root.stat().st_mode & 0o222:
            raise ActivationContractError(
                f"immutable artifact directory is writable: {immutable_root}"
            )
    return ValidatedRelease(
        release_dir=release_dir,
        release_id=manifest["release_id"],
        release_digest=digest,
        manifest=manifest,
        host_plist=release_dir / "services" / "com.hermes.host.plist",
        connector_plist=release_dir / "services" / "com.hermes.connector.plist",
    )


def _canonical_directory(path: Path, label: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise ActivationContractError(f"{label} path must be absolute and canonical")
    if not path.is_dir() or path.is_symlink():
        raise ActivationContractError(f"{label} directory is missing or unsafe")
    _reject_symlink_components(path)
    return path


def _private_json_file(path: Path, *, mode: int) -> Mapping[str, Any]:
    _regular_file(path, mode)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationContractError(f"invalid JSON artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise ActivationContractError(f"invalid JSON artifact: {path.name}")
    return value


def _digest_file(path: Path, expected: Any, mode: int | None) -> None:
    _regular_file(path, mode)
    if (
        not isinstance(expected, str)
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected
    ):
        raise ActivationContractError(f"artifact digest mismatch: {path.name}")


def _regular_file(path: Path, mode: int | None) -> None:
    if path.is_symlink():
        raise ActivationContractError(f"symlink artifact rejected: {path}")
    _reject_symlink_components(path.parent)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ActivationContractError(f"artifact is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ActivationContractError(f"artifact is not a regular file: {path}")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise ActivationContractError(f"artifact has unsafe mode: {path.name}")


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ActivationContractError(
                f"symlink path component rejected: {candidate}"
            )


def _release_file(release_dir: Path, relative: Any) -> Path:
    if not isinstance(relative, (str, Path)):
        raise ActivationContractError("release artifact path is invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ActivationContractError("release artifact path escapes release")
    result = (release_dir / relative_path).resolve(strict=False)
    if not _is_within(result, release_dir):
        raise ActivationContractError("release artifact path escapes release")
    return result


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True
