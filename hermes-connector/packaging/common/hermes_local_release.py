"""Offline, immutable Hermes local release assembly.

This module deliberately has no dependency on the Connector application package.  It is
invoked by release tooling with explicit, content-addressed inputs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STAGING_NONCE_HEX_CHARS = 12
_SIGNED_PLUGIN_FIELDS = {
    "schema_version",
    "plugin_id",
    "version",
    "wheel_path",
    "wheel_sha256",
    "store_root",
    "entrypoint",
    "signature_algorithm",
    "key_id",
    "issued_at",
    "expires_at",
    "signature",
}
_PLUGIN_ENTRYPOINT_FIELDS = {"group", "name", "value"}
_TRUST_STORE_FIELDS = {"schema_version", "keys"}
_TRUST_KEY_FIELDS = {
    "key_id",
    "signature_algorithm",
    "public_key",
    "not_before",
    "not_after",
}
_VERIFY_RUNTIME = r"""
import importlib.metadata
import importlib.util
import json
import pathlib
import sys

module_name, console_name, expected_entrypoint, expected_project = sys.argv[1:]
spec = importlib.util.find_spec(module_name)
if spec is None or spec.origin is None:
    raise SystemExit(f"module not found: {module_name}")
matches = [ep for ep in importlib.metadata.entry_points(group="console_scripts") if ep.name == console_name]
if len(matches) != 1 or matches[0].value != expected_entrypoint:
    raise SystemExit(f"console entrypoint mismatch: {console_name}")
console_path = pathlib.Path(sys.executable).parent / console_name
if not console_path.is_file() or console_path.is_symlink():
    raise SystemExit(f"console script missing or symlinked: {console_path}")
site_roots = [pathlib.Path(value).resolve() for value in sys.path if "site-packages" in value]
project_key = expected_project.lower().replace("-", "_").replace(".", "_")
unexpected_direct_urls = []
for direct_url in (item for root in site_roots for item in root.glob("*.dist-info/direct_url.json")):
    if not direct_url.parent.name.lower().startswith(project_key + "-"):
        unexpected_direct_urls.append(str(direct_url))
pth_escapes = []
for pth in (item for root in site_roots for item in root.glob("*.pth")):
    for line in pth.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or value.startswith("import "):
            continue
        candidate = (pth.parent / value).resolve() if not pathlib.Path(value).is_absolute() else pathlib.Path(value).resolve()
        if not any(candidate == root or root in candidate.parents for root in site_roots):
            pth_escapes.append(str(candidate))
print(json.dumps({
    "module_origin": str(pathlib.Path(spec.origin).resolve()),
    "console_entrypoint": str(console_path.resolve()),
    "unexpected_direct_urls": unexpected_direct_urls,
    "pth_escapes": pth_escapes,
}, sort_keys=True))
""".strip()


class ReleaseBuildError(RuntimeError):
    """The release cannot be built without violating an immutable boundary."""


@dataclass(frozen=True)
class ArtifactInput:
    path: Path
    sha256: str


@dataclass(frozen=True)
class RuntimeReleaseInput:
    project_name: str
    version: str
    wheel: ArtifactInput
    lock: ArtifactInput
    project: ArtifactInput
    console_script: str
    entrypoint: str
    launch_module: str


@dataclass(frozen=True)
class ReleaseInputs:
    release_id: str
    core: RuntimeReleaseInput
    plugin_bundle: ArtifactInput
    plugin_store_manifest: ArtifactInput
    signed_plugin_manifest: Mapping[str, Any]
    connector: RuntimeReleaseInput


@dataclass(frozen=True)
class BuildCommand:
    purpose: str
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    release_dir: Path


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""


class CommandRunner(Protocol):
    def run(self, command: BuildCommand) -> CommandResult: ...


class SubprocessRunner:
    """Run an already-audited command without a shell."""

    def run(self, command: BuildCommand) -> CommandResult:
        environment = os.environ.copy()
        environment.update(command.environment)
        completed = subprocess.run(
            command.argv,
            cwd=command.cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return CommandResult(stdout=completed.stdout)


@dataclass(frozen=True)
class ReleasePlan:
    release_id: str
    release_dir: Path
    release_digest: str
    commands: tuple[BuildCommand, ...]


@dataclass(frozen=True)
class PublishedRelease(ReleasePlan):
    reused: bool


class ReleaseBuilder:
    def __init__(
        self,
        *,
        releases_root: Path,
        runner: CommandRunner | None = None,
        service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
    ) -> None:
        self._root = Path(releases_root)
        self._runner = runner or SubprocessRunner()
        self._service_renderer = service_renderer

    def build(
        self, inputs: ReleaseInputs, *, dry_run: bool = False
    ) -> ReleasePlan | PublishedRelease:
        self._validate_inputs(inputs)
        release_dir = self._root / inputs.release_id
        services = self._render_services(release_dir)
        digest = _release_digest(inputs, services)
        existing = self._read_existing(release_dir)
        if existing is not None:
            if existing.get("release_digest") != digest:
                raise ReleaseBuildError(
                    "release id already exists with a different digest"
                )
            payload = _release_payload(inputs, services)
            if any(existing.get(key) != value for key, value in payload.items()):
                raise ReleaseBuildError(
                    "existing release manifest conflicts with declared inputs"
                )
            self._validate_existing_layout(release_dir)
            expected_receipt = _command_receipt(self._commands(inputs, release_dir))
            self._validate_existing_artifacts(
                release_dir, inputs, services, expected_receipt
            )
            return PublishedRelease(inputs.release_id, release_dir, digest, (), True)

        if dry_run:
            return ReleasePlan(
                release_id=inputs.release_id,
                release_dir=release_dir,
                release_digest=digest,
                commands=self._commands(inputs, release_dir),
            )

        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_symlink_components(self._root)
        staging_nonce = uuid.uuid4().hex[:_STAGING_NONCE_HEX_CHARS]
        staging = self._root / f".{inputs.release_id}.staging.{staging_nonce}"
        try:
            self._prepare_staging(staging, inputs, services)
            commands = self._commands(inputs, staging)
            verification = self._execute(commands, staging, release_dir)
            _relocate_posix_console_launchers(staging, release_dir, inputs)
            logical_commands = _logical_commands(commands, staging, release_dir)
            receipt = _command_receipt(logical_commands)
            receipt_path = staging / "receipts" / "build-commands.json"
            _write_private_json(receipt_path, receipt)
            manifest = _manifest(
                inputs,
                digest,
                verification,
                services,
                _sha256(receipt_path),
            )
            _write_private_json(staging / "manifest" / "release.json", manifest)
            for immutable_root in (
                staging / "plugin",
                staging / "receipts" / "inputs",
                staging / "host" / "project",
                staging / "connector" / "project",
            ):
                _freeze_tree(immutable_root)
            if release_dir.exists() or release_dir.is_symlink():
                raise ReleaseBuildError("release destination appeared during build")
            os.replace(staging, release_dir)
            return PublishedRelease(
                inputs.release_id, release_dir, digest, logical_commands, False
            )
        except Exception as exc:
            if staging.exists() and not staging.is_symlink():
                remove_frozen_tree(staging)
            if isinstance(exc, ReleaseBuildError):
                raise
            raise ReleaseBuildError(str(exc)) from exc

    def _validate_inputs(self, inputs: ReleaseInputs) -> None:
        if not _RELEASE_ID.fullmatch(inputs.release_id) or inputs.release_id in {
            ".",
            "..",
        }:
            raise ReleaseBuildError("invalid release_id")
        if not self._root.is_absolute():
            raise ReleaseBuildError("releases_root must be absolute")
        if self._root.exists() or self._root.is_symlink():
            _reject_symlink_components(self._root)
        for name, artifact in _artifacts(inputs).items():
            _validate_artifact(name, artifact)
        _validate_runtime("core", inputs.core)
        _validate_runtime("connector", inputs.connector)
        _reject_unsafe_dependency_sources(inputs.core)
        _reject_unsafe_dependency_sources(inputs.connector)
        _validate_plugin_signature(inputs, self._root / inputs.release_id)

    def _render_services(self, release_dir: Path) -> Mapping[str, bytes]:
        if self._service_renderer is None:
            return MappingProxyType({})
        try:
            rendered = self._service_renderer(release_dir)
        except Exception as exc:
            raise ReleaseBuildError(
                f"service template rendering failed: {exc}"
            ) from exc
        validated: dict[str, bytes] = {}
        for name, payload in rendered.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
                raise ReleaseBuildError("invalid service template name")
            if not isinstance(payload, bytes) or not payload:
                raise ReleaseBuildError("service template must be non-empty bytes")
            validated[f"com.hermes.{name}.plist"] = payload
        if not validated:
            raise ReleaseBuildError("service renderer returned no templates")
        return MappingProxyType(validated)

    def _read_existing(self, release_dir: Path) -> Mapping[str, Any] | None:
        if not release_dir.exists() and not release_dir.is_symlink():
            return None
        _reject_symlink_components(release_dir)
        path = release_dir / "manifest" / "release.json"
        if not path.is_file() or path.is_symlink():
            raise ReleaseBuildError("existing release has no trusted manifest")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("existing release manifest is unreadable") from exc
        if not isinstance(value, dict):
            raise ReleaseBuildError("existing release manifest is invalid")
        return value

    @staticmethod
    def _validate_existing_layout(release_dir: Path) -> None:
        for relative in (
            "manifest",
            "host/venv",
            "plugin/artifacts",
            "plugin/metadata",
            "connector/venv",
            "services",
            "receipts",
        ):
            path = release_dir / relative
            if not path.is_dir() or path.is_symlink():
                raise ReleaseBuildError(f"existing release is incomplete: {relative}")

    @staticmethod
    def _validate_existing_artifacts(
        release_dir: Path,
        inputs: ReleaseInputs,
        services: Mapping[str, bytes],
        expected_receipt: Mapping[str, Any],
    ) -> None:
        expected = {
            release_dir
            / "host"
            / "project"
            / "pyproject.toml": inputs.core.project.sha256,
            release_dir / "host" / "project" / "uv.lock": inputs.core.lock.sha256,
            release_dir
            / "receipts"
            / "inputs"
            / "core"
            / inputs.core.wheel.path.name: inputs.core.wheel.sha256,
            release_dir
            / "plugin"
            / "artifacts"
            / "hermes-agent-plugin"
            / str(inputs.signed_plugin_manifest["version"])
            / inputs.plugin_bundle.sha256
            / inputs.plugin_bundle.path.name: inputs.plugin_bundle.sha256,
            release_dir
            / "plugin"
            / "metadata"
            / "trust-store.json": inputs.plugin_store_manifest.sha256,
            release_dir
            / "connector"
            / "project"
            / "pyproject.toml": inputs.connector.project.sha256,
            release_dir
            / "connector"
            / "project"
            / "uv.lock": inputs.connector.lock.sha256,
            release_dir
            / "receipts"
            / "inputs"
            / "connector"
            / inputs.connector.wheel.path.name: inputs.connector.wheel.sha256,
        }
        for path, digest in expected.items():
            if not path.is_file() or path.is_symlink() or _sha256(path) != digest:
                raise ReleaseBuildError(
                    f"published artifact digest mismatch: {path.name}"
                )
            if path.stat().st_mode & 0o222:
                raise ReleaseBuildError(f"published artifact is writable: {path.name}")
        for name, payload in services.items():
            path = release_dir / "services" / name
            if (
                not path.is_file()
                or path.is_symlink()
                or _sha256(path) != hashlib.sha256(payload).hexdigest()
            ):
                raise ReleaseBuildError(f"published artifact digest mismatch: {name}")
        signed_path = (
            release_dir / "plugin" / "metadata" / "signed-plugin-manifest.json"
        )
        try:
            signed = json.loads(signed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError(
                "published artifact digest mismatch: signed-plugin-manifest.json"
            ) from exc
        if (
            signed != inputs.signed_plugin_manifest
            or signed_path.is_symlink()
            or signed_path.stat().st_mode & 0o222
        ):
            raise ReleaseBuildError(
                "published artifact digest mismatch: signed-plugin-manifest.json"
            )
        receipt_path = release_dir / "receipts" / "build-commands.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError(
                "published artifact digest mismatch: build-commands.json"
            ) from exc
        if receipt != expected_receipt or receipt_path.is_symlink():
            raise ReleaseBuildError(
                "published artifact digest mismatch: build-commands.json"
            )

    @staticmethod
    def _prepare_staging(
        staging: Path,
        inputs: ReleaseInputs,
        services: Mapping[str, bytes],
    ) -> None:
        staging.mkdir(mode=0o700)
        for relative in (
            "manifest",
            "host/venv",
            "host/project",
            "plugin/artifacts",
            "plugin/metadata",
            "connector/venv",
            "connector/project",
            "services",
            "receipts/inputs/core",
            "receipts/inputs/connector",
            "receipts/logs",
        ):
            (staging / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        _copy_verified(
            inputs.core.project, staging / "host" / "project" / "pyproject.toml"
        )
        _copy_verified(inputs.core.lock, staging / "host" / "project" / "uv.lock")
        _copy_verified(
            inputs.core.wheel,
            staging / "receipts" / "inputs" / "core" / inputs.core.wheel.path.name,
        )
        _copy_verified(
            inputs.connector.project,
            staging / "connector" / "project" / "pyproject.toml",
        )
        _copy_verified(
            inputs.connector.lock, staging / "connector" / "project" / "uv.lock"
        )
        _copy_verified(
            inputs.connector.wheel,
            staging
            / "receipts"
            / "inputs"
            / "connector"
            / inputs.connector.wheel.path.name,
        )
        _copy_verified(
            inputs.plugin_bundle,
            staging
            / "plugin"
            / "artifacts"
            / "hermes-agent-plugin"
            / str(inputs.signed_plugin_manifest["version"])
            / inputs.plugin_bundle.sha256
            / inputs.plugin_bundle.path.name,
            mode=0o400,
        )
        _copy_verified(
            inputs.plugin_store_manifest,
            staging / "plugin" / "metadata" / "trust-store.json",
            mode=0o400,
        )
        _write_private_json(
            staging / "plugin" / "metadata" / "signed-plugin-manifest.json",
            inputs.signed_plugin_manifest,
            mode=0o400,
        )
        for name, payload in services.items():
            _write_private_bytes(staging / "services" / name, payload)

    @staticmethod
    def _commands(inputs: ReleaseInputs, release_dir: Path) -> tuple[BuildCommand, ...]:
        host_project = release_dir / "host" / "project"
        connector_project = release_dir / "connector" / "project"
        host_venv = release_dir / "host" / "venv"
        connector_venv = release_dir / "connector" / "venv"
        host_wheel = (
            release_dir / "receipts" / "inputs" / "core" / inputs.core.wheel.path.name
        )
        connector_wheel = (
            release_dir
            / "receipts"
            / "inputs"
            / "connector"
            / inputs.connector.wheel.path.name
        )

        def command(
            purpose: str, argv: tuple[str, ...], cwd: Path, venv: Path
        ) -> BuildCommand:
            return BuildCommand(
                purpose=purpose,
                argv=argv,
                cwd=cwd,
                environment=MappingProxyType(
                    {"UV_OFFLINE": "1", "UV_PROJECT_ENVIRONMENT": str(venv)}
                ),
                release_dir=release_dir,
            )

        return (
            command(
                "sync-host-dependencies",
                (
                    "uv",
                    "sync",
                    "--offline",
                    "--project",
                    str(host_project),
                    "--locked",
                    "--no-install-project",
                ),
                host_project,
                host_venv,
            ),
            command(
                "install-final-core-wheel",
                (
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(host_venv / "bin" / "python"),
                    "--no-deps",
                    str(host_wheel),
                ),
                release_dir,
                host_venv,
            ),
            command(
                "verify-host-runtime",
                (
                    str(host_venv / "bin" / "python"),
                    "-I",
                    "-c",
                    _VERIFY_RUNTIME,
                    inputs.core.launch_module,
                    inputs.core.console_script,
                    inputs.core.entrypoint,
                    inputs.core.project_name,
                ),
                release_dir,
                host_venv,
            ),
            command(
                "sync-connector-dependencies",
                (
                    "uv",
                    "sync",
                    "--offline",
                    "--project",
                    str(connector_project),
                    "--locked",
                    "--no-install-project",
                ),
                connector_project,
                connector_venv,
            ),
            command(
                "install-final-connector-wheel",
                (
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(connector_venv / "bin" / "python"),
                    "--no-deps",
                    str(connector_wheel),
                ),
                release_dir,
                connector_venv,
            ),
            command(
                "verify-connector-runtime",
                (
                    str(connector_venv / "bin" / "python"),
                    "-I",
                    "-c",
                    _VERIFY_RUNTIME,
                    inputs.connector.launch_module,
                    inputs.connector.console_script,
                    inputs.connector.entrypoint,
                    inputs.connector.project_name,
                ),
                release_dir,
                connector_venv,
            ),
        )

    def _execute(
        self,
        commands: tuple[BuildCommand, ...],
        staging: Path,
        release_dir: Path,
    ) -> dict[str, Mapping[str, Any]]:
        verification: dict[str, Mapping[str, Any]] = {}
        for command in commands:
            result = self._runner.run(command)
            if command.purpose.startswith("verify-"):
                runtime = (
                    "host" if command.purpose == "verify-host-runtime" else "connector"
                )
                verification[runtime] = _validate_verification(
                    result.stdout,
                    staging / runtime / "venv",
                    release_dir / runtime / "venv",
                    command.argv[-3],
                )
        if set(verification) != {"host", "connector"}:
            raise ReleaseBuildError("runtime verification receipts are incomplete")
        return verification


def _validate_runtime(name: str, runtime: RuntimeReleaseInput) -> None:
    for field_name, value in (
        ("project_name", runtime.project_name),
        ("version", runtime.version),
        ("console_script", runtime.console_script),
        ("entrypoint", runtime.entrypoint),
        ("launch_module", runtime.launch_module),
    ):
        if not value or value != value.strip() or "\x00" in value:
            raise ReleaseBuildError(f"invalid {name} {field_name}")
    if "/" in runtime.console_script or runtime.console_script in {".", ".."}:
        raise ReleaseBuildError(f"invalid {name} console_script")


def _logical_commands(
    commands: tuple[BuildCommand, ...],
    staging: Path,
    release_dir: Path,
) -> tuple[BuildCommand, ...]:
    staging_text = str(staging)
    release_text = str(release_dir)

    def logical(value: str) -> str:
        return value.replace(staging_text, release_text)

    return tuple(
        BuildCommand(
            purpose=command.purpose,
            argv=tuple(logical(value) for value in command.argv),
            cwd=Path(logical(str(command.cwd))),
            environment=MappingProxyType(
                {key: logical(value) for key, value in command.environment.items()}
            ),
            release_dir=release_dir,
        )
        for command in commands
    )


def _relocate_posix_console_launchers(
    staging: Path,
    release_dir: Path,
    inputs: ReleaseInputs,
) -> None:
    if os.name == "nt":
        return
    for runtime, runtime_input in (
        ("host", inputs.core),
        ("connector", inputs.connector),
    ):
        launcher = staging / runtime / "venv" / "bin" / runtime_input.console_script
        if not launcher.exists():
            continue
        if launcher.is_symlink() or not launcher.is_file():
            raise ReleaseBuildError("staged POSIX console launcher is unavailable")
        staged_python = str(staging / runtime / "venv" / "bin" / "python").encode()
        final_python = str(release_dir / runtime / "venv" / "bin" / "python").encode()
        content = launcher.read_bytes()
        if content.count(staged_python) != 1:
            raise ReleaseBuildError("staged POSIX console launcher is not relocatable")
        launcher.write_bytes(content.replace(staged_python, final_python))


def _command_receipt(commands: tuple[BuildCommand, ...]) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "commands": [
            {
                "purpose": command.purpose,
                "argv": list(command.argv),
                "cwd": str(command.cwd),
                "environment": dict(sorted(command.environment.items())),
                "status": "succeeded",
            }
            for command in commands
        ],
    }


def _validate_plugin_signature(inputs: ReleaseInputs, release_dir: Path) -> None:
    manifest = inputs.signed_plugin_manifest
    if not isinstance(manifest, Mapping) or set(manifest) != _SIGNED_PLUGIN_FIELDS:
        raise ReleaseBuildError("signed_plugin_manifest does not match schema v1")
    try:
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ReleaseBuildError(
            "signed_plugin_manifest must be canonical JSON data"
        ) from exc
    if manifest["schema_version"] != 1:
        raise ReleaseBuildError("invalid signed_plugin_manifest schema_version")
    if manifest["plugin_id"] != "hermes-agent-plugin":
        raise ReleaseBuildError("invalid signed_plugin_manifest plugin_id")
    if not isinstance(manifest["version"], str) or not manifest["version"].strip():
        raise ReleaseBuildError("invalid signed_plugin_manifest version")
    if manifest["signature_algorithm"] != "ed25519":
        raise ReleaseBuildError("invalid signature_algorithm")
    entrypoint = manifest["entrypoint"]
    if (
        not isinstance(entrypoint, Mapping)
        or set(entrypoint) != _PLUGIN_ENTRYPOINT_FIELDS
    ):
        raise ReleaseBuildError("invalid Plugin entrypoint declaration")
    if dict(entrypoint) != {
        "group": "hermes_agent.plugins",
        "name": "hermes-agent-plugin",
        "value": "hermes_agent_plugin",
    }:
        raise ReleaseBuildError("invalid Plugin entrypoint declaration")

    immutable_artifacts = (release_dir / "plugin" / "artifacts").resolve(strict=False)
    expected_wheel = (
        immutable_artifacts
        / "hermes-agent-plugin"
        / str(manifest["version"])
        / inputs.plugin_bundle.sha256
        / inputs.plugin_bundle.path.name
    )
    _reject_symlink_components(immutable_artifacts)
    if manifest["wheel_path"] != str(expected_wheel):
        raise ReleaseBuildError(
            "Plugin wheel_path is not the exact immutable artifact path"
        )
    store_root_raw = manifest["store_root"]
    if not isinstance(store_root_raw, str):
        raise ReleaseBuildError("Plugin store_root must be an absolute canonical path")
    store_root = Path(store_root_raw)
    if not store_root.is_absolute() or store_root != store_root.resolve(strict=False):
        raise ReleaseBuildError("Plugin store_root must be an absolute canonical path")
    release_resolved = release_dir.resolve(strict=False)
    if store_root == release_resolved or release_resolved in store_root.parents:
        raise ReleaseBuildError(
            "Plugin store_root must remain outside immutable release"
        )
    _reject_symlink_components(store_root)
    if store_root.exists():
        metadata = store_root.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ReleaseBuildError(
                "Plugin store_root must be a private 0700 directory"
            )
    if manifest["wheel_sha256"] != inputs.plugin_bundle.sha256:
        raise ReleaseBuildError("Plugin wheel_sha256 does not match bundle")
    _decode_base64_exact(manifest["signature"], 64, "signature")

    issued_at = _parse_utc(manifest["issued_at"], "issued_at")
    expires_at = _parse_utc(manifest["expires_at"], "expires_at")
    if issued_at >= expires_at:
        raise ReleaseBuildError("Plugin signature validity window is invalid")

    try:
        trust_store = json.loads(
            inputs.plugin_store_manifest.path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("Plugin trust store is not valid JSON") from exc
    if not isinstance(trust_store, dict) or set(trust_store) != _TRUST_STORE_FIELDS:
        raise ReleaseBuildError("Plugin trust store does not match schema v1")
    if trust_store["schema_version"] != 1 or not isinstance(trust_store["keys"], list):
        raise ReleaseBuildError("Plugin trust store does not match schema v1")

    trusted_keys: dict[str, tuple[datetime, datetime]] = {}
    for key in trust_store["keys"]:
        if not isinstance(key, dict) or set(key) != _TRUST_KEY_FIELDS:
            raise ReleaseBuildError("Plugin trust key does not match schema v1")
        key_id = key["key_id"]
        if not isinstance(key_id, str) or not key_id or key_id in trusted_keys:
            raise ReleaseBuildError("Plugin trust key_id is invalid or duplicated")
        if key["signature_algorithm"] != "ed25519":
            raise ReleaseBuildError("Plugin trust key signature_algorithm is invalid")
        _decode_base64_exact(key["public_key"], 32, "public_key")
        not_before = _parse_utc(key["not_before"], "not_before")
        not_after = _parse_utc(key["not_after"], "not_after")
        if not_before >= not_after:
            raise ReleaseBuildError("Plugin trust key validity window is invalid")
        trusted_keys[key_id] = (not_before, not_after)

    key_id = manifest["key_id"]
    if not isinstance(key_id, str) or key_id not in trusted_keys:
        raise ReleaseBuildError("Plugin signature does not reference a trusted key")
    not_before, not_after = trusted_keys[key_id]
    if issued_at < not_before or expires_at > not_after:
        raise ReleaseBuildError("Plugin signature validity window exceeds trusted key")


def _decode_base64_exact(value: Any, expected_size: int, field: str) -> bytes:
    if not isinstance(value, str):
        raise ReleaseBuildError(f"Plugin {field} must be base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseBuildError(f"Plugin {field} must be base64") from exc
    if len(decoded) != expected_size:
        raise ReleaseBuildError(f"Plugin {field} has invalid length")
    return decoded


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseBuildError(f"Plugin {field} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReleaseBuildError(f"Plugin {field} must be RFC3339 UTC") from exc
    if parsed.tzinfo != UTC:
        raise ReleaseBuildError(f"Plugin {field} must be RFC3339 UTC")
    return parsed


def _validate_artifact(name: str, artifact: ArtifactInput) -> None:
    path = Path(artifact.path)
    if not _SHA256.fullmatch(artifact.sha256):
        raise ReleaseBuildError(f"invalid sha256 declaration for {name}")
    if path.is_symlink():
        raise ReleaseBuildError(f"symlink input rejected: {name}")
    _reject_symlink_components(path.parent)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ReleaseBuildError(f"missing input artifact: {name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBuildError(f"input artifact is not a regular file: {name}")
    actual = _sha256(path)
    if actual != artifact.sha256:
        raise ReleaseBuildError(f"sha256 mismatch for {name}")


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ReleaseBuildError(f"symlink path component rejected: {candidate}")


def _reject_unsafe_dependency_sources(runtime: RuntimeReleaseInput) -> None:
    try:
        project = tomllib.loads(runtime.project.path.read_text(encoding="utf-8"))
        lock = tomllib.loads(runtime.lock.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseBuildError(
            f"invalid project or lock snapshot for {runtime.project_name}"
        ) from exc

    unsafe_project = _find_source_marker(project)
    if unsafe_project:
        label = (
            "direct URL"
            if unsafe_project in {"url", "git", "path", "direct_url"}
            else "editable"
        )
        raise ReleaseBuildError(
            f"{label} dependency source rejected for {runtime.project_name}"
        )

    normalized_project = runtime.project_name.lower().replace("_", "-")
    packages = lock.get("package", [])
    if not isinstance(packages, list):
        raise ReleaseBuildError(f"invalid lock snapshot for {runtime.project_name}")
    for package in packages:
        if not isinstance(package, dict):
            raise ReleaseBuildError(f"invalid lock package for {runtime.project_name}")
        source = package.get("source", {})
        if not isinstance(source, dict):
            continue
        package_name = str(package.get("name", "")).lower().replace("_", "-")
        if "editable" in source:
            if package_name == normalized_project and source["editable"] == ".":
                continue
            raise ReleaseBuildError(
                f"editable dependency source rejected for {runtime.project_name}"
            )
        if any(key in source for key in ("url", "git", "path", "direct_url")):
            raise ReleaseBuildError(
                f"direct URL dependency source rejected for {runtime.project_name}"
            )


def _find_source_marker(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "editable" and bool(child):
                return key
            if key in {"url", "git", "path", "direct_url"}:
                return key
            found = _find_source_marker(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_source_marker(child)
            if found:
                return found
    elif isinstance(value, str) and re.search(r"@\s*(?:file|https?|git\+)://", value):
        return "url"
    return None


def _validate_verification(
    stdout: str,
    staging_venv: Path,
    final_venv: Path,
    console_script: str,
    *,
    platform_name: str | None = None,
) -> Mapping[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseBuildError(
            "runtime verification did not return valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ReleaseBuildError("runtime verification receipt is invalid")
    if value.get("unexpected_direct_urls") or value.get("pth_escapes"):
        raise ReleaseBuildError("editable or direct_url installation detected")
    module_origin = _path_inside(value.get("module_origin"), staging_venv)
    console = _path_inside(value.get("console_entrypoint"), staging_venv)
    if (platform_name or os.name) == "nt":
        console_root = staging_venv / "Scripts"
        expected_consoles = {
            (console_root / console_script).resolve(strict=False),
            (console_root / f"{console_script}.exe").resolve(strict=False),
        }
    else:
        expected_consoles = {
            (staging_venv / "bin" / console_script).resolve(strict=False)
        }
    if console not in expected_consoles:
        raise ReleaseBuildError(
            "console entrypoint is not the exact isolated venv executable"
        )
    return {
        "module_origin": str(
            final_venv / module_origin.relative_to(staging_venv.resolve(strict=False))
        ),
        "console_entrypoint": str(
            final_venv / console.relative_to(staging_venv.resolve(strict=False))
        ),
        "unexpected_direct_urls": [],
        "pth_escapes": [],
    }


def _path_inside(raw: Any, root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ReleaseBuildError("runtime verification path is missing")
    path = Path(raw).resolve(strict=False)
    expected = root.resolve(strict=False)
    try:
        path.relative_to(expected)
    except ValueError as exc:
        raise ReleaseBuildError(
            "runtime verification reported path outside isolated venv"
        ) from exc
    return path


def _copy_verified(
    artifact: ArtifactInput,
    destination: Path,
    *,
    mode: int = 0o600,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with artifact.path.open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
    destination.chmod(mode)
    if _sha256(destination) != artifact.sha256:
        raise ReleaseBuildError(f"sha256 mismatch while staging {artifact.path.name}")


def _write_private_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    mode: int = 0o600,
) -> None:
    encoded = (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        + b"\n"
    )
    _write_private_bytes(path, encoded, mode=mode)


def _write_private_bytes(path: Path, encoded: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise ReleaseBuildError(f"symlink cannot enter immutable tree: {path}")
        if path.is_dir():
            path.chmod(0o500)
        elif path.is_file():
            path.chmod(0o400)
        else:
            raise ReleaseBuildError(f"non-regular immutable artifact rejected: {path}")
    root.chmod(0o500)


def remove_frozen_tree(root: Path) -> None:
    root = Path(root)
    if root.is_symlink():
        raise ReleaseBuildError("refusing to clean a symlinked release tree")
    if not root.exists():
        return

    root.chmod(0o700)
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        for name in names:
            path = current / name
            if not path.is_symlink():
                path.chmod(0o700)
        for name in files:
            path = current / name
            if not path.is_symlink():
                path.chmod(0o600)

    def retry_writable(function, path, _exception):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(root, onexc=retry_writable)


def _artifacts(inputs: ReleaseInputs) -> Mapping[str, ArtifactInput]:
    return {
        "core.wheel": inputs.core.wheel,
        "core.lock": inputs.core.lock,
        "core.project": inputs.core.project,
        "plugin.bundle": inputs.plugin_bundle,
        "plugin.store_manifest": inputs.plugin_store_manifest,
        "connector.wheel": inputs.connector.wheel,
        "connector.lock": inputs.connector.lock,
        "connector.project": inputs.connector.project,
    }


def _runtime_manifest(runtime: RuntimeReleaseInput) -> Mapping[str, Any]:
    return {
        "project_name": runtime.project_name,
        "version": runtime.version,
        "wheel_path": runtime.wheel.path.name,
        "wheel_sha256": runtime.wheel.sha256,
        "project_path": "pyproject.toml",
        "project_sha256": runtime.project.sha256,
        "lock_path": "uv.lock",
        "lock_sha256": runtime.lock.sha256,
        "console_script": runtime.console_script,
        "entrypoint": runtime.entrypoint,
        "launch_module": runtime.launch_module,
    }


def _release_payload(
    inputs: ReleaseInputs,
    services: Mapping[str, bytes],
) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "release_id": inputs.release_id,
        "core": _runtime_manifest(inputs.core),
        "connector": _runtime_manifest(inputs.connector),
        "plugin": {
            "bundle_path": (
                "plugin/artifacts/hermes-agent-plugin/"
                f"{inputs.signed_plugin_manifest['version']}/"
                f"{inputs.plugin_bundle.sha256}/{inputs.plugin_bundle.path.name}"
            ),
            "bundle_sha256": inputs.plugin_bundle.sha256,
            "signed_manifest_path": "plugin/metadata/signed-plugin-manifest.json",
            "trust_store_path": "plugin/metadata/trust-store.json",
            "store_manifest_sha256": inputs.plugin_store_manifest.sha256,
        },
        "signed_plugin_manifest": inputs.signed_plugin_manifest,
        "services": {
            name: {"sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in sorted(services.items())
        },
    }


def _release_digest(inputs: ReleaseInputs, services: Mapping[str, bytes]) -> str:
    encoded = json.dumps(
        _release_payload(inputs, services),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest(
    inputs: ReleaseInputs,
    release_digest: str,
    verification: Mapping[str, Mapping[str, Any]],
    services: Mapping[str, bytes],
    receipt_sha256: str,
) -> Mapping[str, Any]:
    value = dict(_release_payload(inputs, services))
    value["release_digest"] = release_digest
    value["verification"] = verification
    value["receipts"] = {
        "build_commands_path": "receipts/build-commands.json",
        "build_commands_sha256": receipt_sha256,
    }
    value["build_policy"] = {
        "offline": True,
        "locked": True,
        "install_project": False,
        "final_wheel_no_deps": True,
        "plugin_installed_in_host_venv": False,
    }
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
