#!/usr/bin/env python3
"""Verify a Hermes Desktop bootstrap qualification payload before artifact upload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from assemble_bootstrap_payload import (
    BootstrapPayloadError,
    TARGETS,
    canonical_json,
    executable_version,
    load_json,
    require_binary,
    safe_relative,
    sha256_file,
    validate_qualified_toolchain,
)

_PAIRING_PLATFORMS = {"macos", "windows"}
_PAIRING_MANIFEST_FIELDS = {
    "schema_version",
    "scope",
    "target",
    "platform",
    "architecture",
    "python_tag",
    "connector_version",
    "connector_lock_sha256",
    "connector_wheel",
    "entrypoint_module",
    "allowed_actions",
    "credential_authority",
    "network_dependency_install",
    "artifacts",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WHEEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\.whl\Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--expected-target", choices=sorted(TARGETS), required=True)
    args = parser.parse_args()

    root = args.payload.resolve()
    if root.is_symlink() or not root.is_dir():
        raise BootstrapPayloadError("bootstrap payload must be a regular directory")
    manifest = load_json(root / "BOOTSTRAP-PAYLOAD.json")
    platform_name, architecture = TARGETS[args.expected_target]

    require_equal(manifest.get("schema_version"), 1, "payload schema")
    require_equal(manifest.get("scope"), "hermes_desktop_bootstrap_qualification", "payload scope")
    require_equal(manifest.get("publication_state"), "qualification-only-unsigned", "publication state")
    require_equal(manifest.get("target"), args.expected_target, "payload target")
    require_equal(manifest.get("platform"), platform_name, "payload platform")
    require_equal(manifest.get("architecture"), architecture, "payload architecture")
    if manifest.get("managed_release") != {
        "included": False,
        "required_next_gate": "DESKTOP-020B2",
    }:
        raise BootstrapPayloadError("B1 payload must not claim the Managed Release is included")
    signing = manifest.get("signing")
    if signing != {"signed": False, "required_next_gate": "DESKTOP-020B3"}:
        raise BootstrapPayloadError("B1 payload must remain explicitly unsigned")
    if manifest.get("update_installation") != {
        "local_managed_release_installer_included": True,
        "requires_private_python": True,
        "requires_system_python": False,
        "requires_network_dependency_install": False,
    }:
        raise BootstrapPayloadError("bootstrap update installation policy is invalid")

    components = manifest.get("components")
    expected_components = {
        "desktop",
        "runtime_manager",
        "toolchain",
        "managed_release_installer",
    }
    pairing_required = platform_name in _PAIRING_PLATFORMS
    if pairing_required:
        expected_components.add("pairing_bootstrap")
    if not isinstance(components, dict) or set(components) != expected_components:
        raise BootstrapPayloadError("bootstrap payload component set is invalid")

    desktop = validate_file_component(root, components["desktop"], "Hermes Desktop", platform_name)
    manager = validate_file_component(root, components["runtime_manager"], "Runtime Manager", platform_name)
    manager_version = executable_version(manager)
    require_equal(manager_version, components["runtime_manager"].get("version"), "Runtime Manager version")
    installer = validate_installer_component(root, components["managed_release_installer"])

    toolchain_component = components["toolchain"]
    if not isinstance(toolchain_component, dict):
        raise BootstrapPayloadError("toolchain component is invalid")
    toolchain_root = root / safe_relative(str(toolchain_component.get("root", "")))
    toolchain = validate_qualified_toolchain(toolchain_root, platform_name, architecture)
    require_equal(toolchain.get("bundle_id"), toolchain_component.get("bundle_id"), "toolchain bundle_id")
    require_equal(toolchain.get("python_version"), toolchain_component.get("python_version"), "Python version")
    require_equal(toolchain.get("uv_version"), toolchain_component.get("uv_version"), "uv version")
    require_equal(len(toolchain.get("files", [])), toolchain_component.get("declared_files"), "toolchain file count")
    require_equal(
        sha256_file(toolchain_root / "TOOLCHAIN-BUNDLE.json"),
        toolchain_component.get("manifest_sha256"),
        "toolchain manifest SHA",
    )
    require_equal(
        sha256_file(toolchain_root / "LICENSE-EVIDENCE.json"),
        toolchain_component.get("license_evidence_sha256"),
        "license evidence SHA",
    )
    require_equal(
        sha256_file(toolchain_root / "UPSTREAM-SOURCE.json"),
        toolchain_component.get("upstream_source_sha256"),
        "upstream source SHA",
    )

    python_path = root / safe_relative(str(toolchain_component.get("python_path", "")))
    uv_path = root / safe_relative(str(toolchain_component.get("uv_path", "")))
    if not python_path.is_relative_to(toolchain_root) or not uv_path.is_relative_to(toolchain_root):
        raise BootstrapPayloadError("private runtime path escapes the toolchain root")

    pairing = None
    if pairing_required:
        if manifest.get("pairing_bootstrap") != {
            "included": True,
            "credential_authority": (
                "macos-keychain" if platform_name == "macos" else "windows-dpapi"
            ),
            "state_authority": "Hermes Connector pairing v1",
            "managed_runtime_required": False,
            "network_dependency_install": False,
        }:
            raise BootstrapPayloadError("pairing bootstrap policy is invalid")
        pairing = validate_pairing_component(
            root,
            components["pairing_bootstrap"],
            expected_target=args.expected_target,
            platform_name=platform_name,
            architecture=architecture,
        )
    elif manifest.get("pairing_bootstrap") is not None:
        raise BootstrapPayloadError("unsupported platform must not claim pairing bootstrap")

    binding = {
        "schema_version": manifest["schema_version"],
        "target": manifest["target"],
        "platform": manifest["platform"],
        "architecture": manifest["architecture"],
        "components": components,
    }
    expected_content_sha = hashlib.sha256(canonical_json(binding)).hexdigest()
    require_equal(expected_content_sha, manifest.get("content_sha256"), "bootstrap content SHA")

    private_python_version, private_python_executable = execute_private_python(
        python_path, toolchain_root
    )
    uv_version = execute_private_uv(uv_path)
    require_equal(private_python_version, toolchain["python_version"], "executed Private Python version")
    if not uv_version.startswith(f"uv {toolchain['uv_version']}"):
        raise BootstrapPayloadError(
            f"executed Private uv version mismatch: expected {toolchain['uv_version']}, got {uv_version!r}"
        )
    execute_installer_help(python_path, installer)
    if pairing is not None:
        execute_pairing_bootstrap_offline(
            private_python=python_path,
            private_uv=uv_path,
            pairing_root=pairing["root"],
            connector_version=pairing["connector_version"],
            platform_name=platform_name,
        )

    result = {
        "schema_version": 1,
        "target": args.expected_target,
        "desktop_binary": str(desktop),
        "runtime_manager_binary": str(manager),
        "runtime_manager_version": manager_version,
        "managed_release_installer": str(installer),
        "managed_release_installer_sha256": sha256_file(installer),
        "toolchain_bundle_id": toolchain["bundle_id"],
        "private_python": private_python_executable,
        "private_python_version": private_python_version,
        "uv_version": uv_version,
        "pairing_bootstrap_verified": pairing is not None,
        "content_sha256": manifest["content_sha256"],
        "managed_release_included": False,
        "signed": False,
        "bootstrap_payload_verified": True,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


def validate_file_component(root: Path, value: Any, label: str, platform_name: str) -> Path:
    if not isinstance(value, dict):
        raise BootstrapPayloadError(f"{label} component is invalid")
    path = root / safe_relative(str(value.get("path", "")))
    require_binary(path.resolve(), label, platform_name)
    require_equal(path.stat().st_size, value.get("size_bytes"), f"{label} size")
    require_equal(sha256_file(path), value.get("sha256"), f"{label} SHA")
    return path.resolve()


def validate_installer_component(root: Path, value: Any) -> Path:
    if not isinstance(value, dict):
        raise BootstrapPayloadError("Managed Release installer component is invalid")
    if value.get("execution") != "private_python_-I_zipapp" or value.get("network_install_allowed") is not False:
        raise BootstrapPayloadError("Managed Release installer execution policy is invalid")
    path = (root / safe_relative(str(value.get("path", "")))).resolve()
    if path.is_symlink() or not path.is_file() or path.suffix != ".pyz":
        raise BootstrapPayloadError("Managed Release installer is missing or symlinked")
    require_equal(path.stat().st_size, value.get("size_bytes"), "Managed Release installer size")
    require_equal(sha256_file(path), value.get("sha256"), "Managed Release installer SHA")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if "__main__.py" not in archive.namelist() or "INSTALLER-MANIFEST.json" not in archive.namelist():
                raise BootstrapPayloadError("Managed Release installer zipapp file set is invalid")
            if archive.testzip() is not None:
                raise BootstrapPayloadError("Managed Release installer zipapp CRC failed")
    except zipfile.BadZipFile as error:
        raise BootstrapPayloadError("Managed Release installer is not a valid zipapp") from error
    return path


def validate_pairing_component(
    root: Path,
    value: Any,
    *,
    expected_target: str,
    platform_name: str,
    architecture: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapPayloadError("pairing bootstrap component is invalid")
    expected_component_fields = {
        "root",
        "manifest_path",
        "manifest_sha256",
        "connector_version",
        "connector_lock_sha256",
        "entrypoint_module",
        "allowed_actions",
        "requires_private_python",
        "requires_system_python",
        "network_dependency_install",
    }
    if set(value) != expected_component_fields:
        raise BootstrapPayloadError("pairing bootstrap component fields are invalid")
    pairing_root = (root / safe_relative(str(value["root"]))).resolve()
    manifest_path = (root / safe_relative(str(value["manifest_path"]))).resolve()
    if not pairing_root.is_relative_to(root) or manifest_path.parent != pairing_root:
        raise BootstrapPayloadError("pairing bootstrap path escapes payload")
    if pairing_root.is_symlink() or not pairing_root.is_dir():
        raise BootstrapPayloadError("pairing bootstrap root is unavailable")
    pairing_manifest = load_json(manifest_path)
    if set(pairing_manifest) != _PAIRING_MANIFEST_FIELDS:
        raise BootstrapPayloadError("pairing bootstrap manifest fields are invalid")
    require_equal(pairing_manifest["schema_version"], 1, "pairing bootstrap schema")
    require_equal(pairing_manifest["scope"], "hermes_desktop_pairing_bootstrap", "pairing bootstrap scope")
    require_equal(pairing_manifest["target"], expected_target, "pairing bootstrap target")
    require_equal(pairing_manifest["platform"], platform_name, "pairing bootstrap platform")
    require_equal(pairing_manifest["architecture"], architecture, "pairing bootstrap architecture")
    require_equal(pairing_manifest["entrypoint_module"], "hermes_connector.cli", "pairing entrypoint")
    require_equal(
        pairing_manifest["allowed_actions"],
        ["pair start", "pair status", "pair cancel"],
        "pairing allowed actions",
    )
    require_equal(pairing_manifest["network_dependency_install"], False, "pairing network policy")
    require_equal(value["requires_private_python"], True, "pairing Private Python policy")
    require_equal(value["requires_system_python"], False, "pairing system Python policy")
    require_equal(value["network_dependency_install"], False, "pairing component network policy")
    require_equal(value["entrypoint_module"], "hermes_connector.cli", "pairing component entrypoint")
    require_equal(value["allowed_actions"], pairing_manifest["allowed_actions"], "pairing component actions")
    require_equal(value["connector_version"], pairing_manifest["connector_version"], "pairing Connector version")
    require_equal(value["connector_lock_sha256"], pairing_manifest["connector_lock_sha256"], "pairing Connector lock")
    require_equal(sha256_file(manifest_path), value["manifest_sha256"], "pairing manifest SHA")

    artifacts = pairing_manifest["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise BootstrapPayloadError("pairing bootstrap artifacts are invalid")
    wheels = pairing_root / "wheels"
    if wheels.is_symlink() or not wheels.is_dir():
        raise BootstrapPayloadError("pairing bootstrap wheels are unavailable")
    declared: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"filename", "sha256", "size_bytes"}:
            raise BootstrapPayloadError("pairing bootstrap artifact entry is invalid")
        filename = item["filename"]
        digest = item["sha256"]
        size = item["size_bytes"]
        if not isinstance(filename, str) or _WHEEL.fullmatch(filename) is None or filename in declared:
            raise BootstrapPayloadError("pairing bootstrap wheel filename is invalid")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise BootstrapPayloadError("pairing bootstrap wheel digest is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise BootstrapPayloadError("pairing bootstrap wheel size is invalid")
        path = wheels / filename
        if path.is_symlink() or not path.is_file():
            raise BootstrapPayloadError("pairing bootstrap wheel is missing")
        require_equal(path.stat().st_size, size, f"pairing wheel size {filename}")
        require_equal(sha256_file(path), digest, f"pairing wheel SHA {filename}")
        declared.add(filename)
    actual = {path.name for path in wheels.iterdir() if path.is_file() or path.is_symlink()}
    if actual != declared:
        raise BootstrapPayloadError("pairing bootstrap wheel set is not closed")
    connector_wheel = pairing_manifest["connector_wheel"]
    if connector_wheel not in declared or not str(connector_wheel).startswith("hermes_connector-"):
        raise BootstrapPayloadError("pairing Connector wheel binding is invalid")
    return {
        "root": pairing_root,
        "connector_version": str(pairing_manifest["connector_version"]),
    }


def execute_private_python(path: Path, toolchain_root: Path) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="hermes-empty-path-") as poison:
        env = sanitized_environment(poison)
        code = (
            "import json,platform,sys; "
            "print(json.dumps({'version': platform.python_version(), 'executable': sys.executable}))"
        )
        completed = subprocess.run(
            [str(path), "-I", "-c", code],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    value = json.loads(completed.stdout.strip())
    executable = Path(str(value["executable"])).resolve()
    if not executable.is_relative_to(toolchain_root.resolve()):
        raise BootstrapPayloadError("executed Private Python escaped the bootstrap toolchain root")
    return str(value["version"]), str(executable)


def execute_private_uv(path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="hermes-empty-path-") as poison:
        completed = subprocess.run(
            [str(path), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env=sanitized_environment(poison),
        )
    value = completed.stdout.strip()
    if not value:
        raise BootstrapPayloadError("Private uv produced no version output")
    return value


def execute_installer_help(private_python: Path, installer: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-empty-path-") as poison:
        completed = subprocess.run(
            [str(private_python), "-I", str(installer), "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env=sanitized_environment(poison),
        )
    output = completed.stdout
    for required in (
        "--payload",
        "--runtime-manager",
        "--qualified-toolchain",
        "--releases-root",
        "--expected-release-id",
        "--expected-target",
    ):
        if required not in output:
            raise BootstrapPayloadError(f"Managed Release installer CLI is missing {required}")


def execute_pairing_bootstrap_offline(
    *,
    private_python: Path,
    private_uv: Path,
    pairing_root: Path,
    connector_version: str,
    platform_name: str,
) -> None:
    wheels = pairing_root / "wheels"
    with tempfile.TemporaryDirectory(prefix="hermes-pairing-offline-") as temporary:
        root = Path(temporary)
        environment_path = root / "pairing-env"
        poison = root / "empty-path"
        poison.mkdir()
        env = sanitized_environment(str(poison))
        env["UV_NO_INDEX"] = "1"
        subprocess.run(
            [
                str(private_uv),
                "venv",
                "--python",
                str(private_python),
                str(environment_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        environment_python = (
            environment_path / "Scripts" / "python.exe"
            if platform_name == "windows"
            else environment_path / "bin" / "python"
        )
        subprocess.run(
            [
                str(private_uv),
                "pip",
                "install",
                "--python",
                str(environment_python),
                "--no-index",
                "--find-links",
                str(wheels),
                f"hermes-connector=={connector_version}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        completed = subprocess.run(
            [str(environment_python), "-I", "-m", "hermes_connector.cli", "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        for action in ("pair start", "pair status", "pair cancel"):
            if action not in completed.stdout:
                raise BootstrapPayloadError(
                    f"pairing bootstrap CLI is missing action {action}"
                )


def sanitized_environment(poison_path: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = poison_path
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_PYTHON",
        "UV_TOOL_BIN_DIR",
        "UV_PROJECT_ENVIRONMENT",
    ):
        env.pop(key, None)
    return env


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise BootstrapPayloadError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        BootstrapPayloadError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as error:
        raise SystemExit(f"bootstrap_payload_validation_error: {error}") from error
