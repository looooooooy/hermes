#!/usr/bin/env python3
"""Verify a Hermes Desktop bootstrap qualification payload before artifact upload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
