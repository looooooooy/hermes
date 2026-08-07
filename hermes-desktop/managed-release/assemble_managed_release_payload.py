#!/usr/bin/env python3
"""Assemble and prove one DESKTOP-020B2 Managed Release payload.

The output is portable input material, not a pre-built venv. CI transiently assembles
an immutable release at a machine-local path using Hermes Private Python/uv, a verified
binary-only wheelhouse, and the Rust vendor-signature verifier. The assembled venv is
used only as proof and is deliberately excluded from the portable payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "hermes-connector" / "packaging" / "common"
sys.path.insert(0, str(COMMON))

from hermes_local_release import (  # noqa: E402
    ArtifactInput,
    ReleaseInputs,
    RuntimeReleaseInput,
)
from hermes_managed_release import ManagedReleaseAssembler  # noqa: E402
from hermes_offline_wheelhouse import load_verified_wheelhouse  # noqa: E402
from hermes_private_toolchain import (  # noqa: E402
    PinnedExecutable,
    PrivateToolchainV1,
)

TARGETS = {
    "macos-aarch64": ("macos", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "linux-x86_64": ("linux", "x86_64"),
    "windows-x86_64": ("windows", "x86_64"),
}
REQUIRED_PURPOSES = {
    "sync-host-dependencies",
    "install-host-runtime",
    "install-agent-plugin",
    "verify-host-runtime",
    "sync-connector-dependencies",
    "install-connector-runtime",
    "verify-connector-runtime",
}


class ManagedPayloadError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--runtime-manager", type=Path, required=True)
    parser.add_argument("--qualified-toolchain", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--core-project", type=Path, required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--core-sdist", type=Path)
    parser.add_argument("--plugin-bundle", type=Path, required=True)
    parser.add_argument("--connector-project", type=Path, required=True)
    parser.add_argument("--connector-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()

    platform_name, architecture = TARGETS[args.target]
    runtime_manager = require_binary(args.runtime_manager.resolve(), "Runtime Manager")
    toolchain_root = args.qualified_toolchain.resolve()
    toolchain = load_private_toolchain(toolchain_root, platform_name, architecture)
    wheelhouse = load_verified_wheelhouse(args.wheelhouse.resolve())
    if wheelhouse.platform != platform_name or wheelhouse.architecture != architecture:
        raise ManagedPayloadError(
            f"wheelhouse target mismatch: expected {platform_name}/{architecture}, "
            f"got {wheelhouse.platform}/{wheelhouse.architecture}"
        )
    if wheelhouse.python_tag != "cp313":
        raise ManagedPayloadError("Managed Release wheelhouse must target cp313")

    core_project = require_project(args.core_project.resolve(), "Core")
    connector_project = require_project(args.connector_project.resolve(), "Connector")
    core_wheel = require_regular(args.core_wheel.resolve(), "Core wheel")
    connector_wheel = require_regular(args.connector_wheel.resolve(), "Connector wheel")
    core_sdist = None if args.core_sdist is None else require_regular(args.core_sdist.resolve(), "Core sdist")
    plugin_root = args.plugin_bundle.resolve()
    portable_manifest_path = require_regular(
        plugin_root / "portable-plugin-manifest.json", "portable Plugin manifest"
    )
    trust_store_path = require_regular(plugin_root / "trust-store.json", "Plugin trust store")
    portable_manifest = load_json(portable_manifest_path)
    plugin_wheel = require_regular(
        plugin_root / "plugin" / str(portable_manifest.get("artifact_filename", "")),
        "Plugin wheel",
    )

    # Cryptographic vendor trust is a Rust Runtime Manager decision, made before the
    # Python release assembler gets authority to install the Plugin wheel.
    plugin_report = run_plugin_verifier(
        runtime_manager, portable_manifest_path, trust_store_path, plugin_wheel
    )
    if plugin_report.get("signature_verified") is not True:
        raise ManagedPayloadError("Runtime Manager did not verify the portable Plugin signature")

    inputs = ReleaseInputs(
        release_id=args.release_id,
        core=RuntimeReleaseInput(
            project_name="hermes-agent",
            version="0.19.0",
            wheel=artifact(core_wheel),
            lock=artifact(core_project / "uv.lock"),
            project=artifact(core_project / "pyproject.toml"),
            console_script="hermes",
            entrypoint="hermes_cli.main:main",
            launch_module="hermes_cli.main",
        ),
        plugin_bundle=artifact(plugin_wheel),
        plugin_store_manifest=artifact(trust_store_path),
        signed_plugin_manifest=portable_manifest,
        connector=RuntimeReleaseInput(
            project_name="hermes-connector",
            version="0.1.0",
            wheel=artifact(connector_wheel),
            lock=artifact(connector_project / "uv.lock"),
            project=artifact(connector_project / "pyproject.toml"),
            console_script="hermes-connector",
            entrypoint="hermes_connector.cli:main",
            launch_module="hermes_connector.cli",
        ),
    )

    if wheelhouse.locks.get("core") != inputs.core.lock.sha256:
        raise ManagedPayloadError("wheelhouse Core lock does not match portable Core project")
    if wheelhouse.locks.get("connector") != inputs.connector.lock.sha256:
        raise ManagedPayloadError("wheelhouse Connector lock does not match portable Connector project")

    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        raise ManagedPayloadError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hermes-managed-proof-", dir=output.parent) as temporary:
        proof_root = Path(temporary).resolve()
        releases_root = proof_root / "releases"

        def portable_verifier(_inputs: ReleaseInputs) -> None:
            report = run_plugin_verifier(
                runtime_manager, portable_manifest_path, trust_store_path, plugin_wheel
            )
            if report.get("signature_verified") is not True:
                raise ManagedPayloadError("portable Plugin signature verification failed during assembly")

        published = ManagedReleaseAssembler(
            releases_root=releases_root,
            toolchain=toolchain,
            wheelhouse=wheelhouse,
            portable_plugin_verifier=portable_verifier,
        ).build(inputs)
        release_dir = published.release_dir.resolve()
        verify_assembled_release(release_dir, published.release_digest, portable_manifest)
        purposes = [command.purpose for command in published.commands]
        if set(purposes) != REQUIRED_PURPOSES or len(purposes) != len(REQUIRED_PURPOSES):
            raise ManagedPayloadError(f"unexpected Managed Release command set: {purposes}")
        for command in published.commands:
            if command.purpose.startswith("sync-") and "--no-default-groups" not in command.argv:
                raise ManagedPayloadError("Managed Release sync command included default dependency groups")

        stage = proof_root / "payload"
        stage.mkdir(mode=0o700)
        copy_file(core_project / "pyproject.toml", stage / "core/project/pyproject.toml")
        copy_file(core_project / "uv.lock", stage / "core/project/uv.lock")
        copy_file(core_wheel, stage / f"core/dist/{core_wheel.name}")
        if core_sdist is not None:
            copy_file(core_sdist, stage / f"core/dist/{core_sdist.name}")
        copy_file(plugin_wheel, stage / f"plugin/{plugin_wheel.name}")
        copy_file(portable_manifest_path, stage / "plugin/portable-plugin-manifest.json")
        copy_file(trust_store_path, stage / "plugin/trust-store.json")
        copy_file(connector_project / "pyproject.toml", stage / "connector/project/pyproject.toml")
        copy_file(connector_project / "uv.lock", stage / "connector/project/uv.lock")
        copy_file(connector_wheel, stage / f"connector/dist/{connector_wheel.name}")
        copy_tree(args.wheelhouse.resolve(), stage / "wheelhouse")

        portable_files = enumerate_files(stage)
        proof = {
            "schema_version": 1,
            "scope": "managed_release_offline_assembly_proof",
            "target": args.target,
            "release_id": published.release_id,
            "release_digest": published.release_digest,
            "command_purposes": purposes,
            "portable_plugin_signature_verified": True,
            "wheelhouse_binary_only": all(item["path"].endswith(".whl") for item in portable_files if item["path"].startswith("wheelhouse/") and item["path"] != "wheelhouse/WHEELHOUSE-MANIFEST.json"),
            "private_toolchain_used": True,
            "network_dependency_install_allowed": False,
            "assembled_release_shipped": False,
        }
        write_json(stage / "ASSEMBLY-PROOF.json", proof)
        payload_files = enumerate_files(stage)
        binding = {
            "schema_version": 1,
            "target": args.target,
            "platform": platform_name,
            "architecture": architecture,
            "release_id": args.release_id,
            "core_version": "0.19.0",
            "plugin_version": str(portable_manifest["version"]),
            "connector_version": "0.1.0",
            "core_lock_sha256": inputs.core.lock.sha256,
            "connector_lock_sha256": inputs.connector.lock.sha256,
            "wheelhouse_manifest_sha256": sha256_file(stage / "wheelhouse/WHEELHOUSE-MANIFEST.json"),
            "portable_plugin_manifest_sha256": sha256_file(stage / "plugin/portable-plugin-manifest.json"),
            "plugin_trust_store_sha256": sha256_file(stage / "plugin/trust-store.json"),
            "files": payload_files,
        }
        manifest = {
            **binding,
            "scope": "hermes_managed_release_portable_inputs",
            "publication_state": "qualification-only-unsigned",
            "content_sha256": hashlib.sha256(canonical_json(binding)).hexdigest(),
            "final_local_assembly_required": True,
            "assembled_venv_included": False,
            "required_next_gate": "DESKTOP-020B3",
        }
        write_json(stage / "MANAGED-RELEASE-PAYLOAD.json", manifest)
        os.replace(stage, output)

    print(
        json.dumps(
            {
                "schema_version": 1,
                "target": args.target,
                "release_id": args.release_id,
                "release_digest": published.release_digest,
                "portable_plugin_signature_verified": True,
                "wheel_count": len(wheelhouse.artifacts),
                "payload_root": str(output),
                "content_sha256": manifest["content_sha256"],
                "assembled": True,
                "assembled_venv_included": False,
            },
            sort_keys=True,
        )
    )
    return 0


def load_private_toolchain(root: Path, platform_name: str, architecture: str) -> PrivateToolchainV1:
    manifest = load_json(require_regular(root / "TOOLCHAIN-BUNDLE.json", "Toolchain manifest"))
    if manifest.get("schema_version") != 1:
        raise ManagedPayloadError("unsupported Toolchain manifest schema")
    if manifest.get("platform") != platform_name or manifest.get("architecture") != architecture:
        raise ManagedPayloadError("Toolchain target does not match Managed Release target")
    for evidence in ("LICENSE-EVIDENCE.json", "UPSTREAM-SOURCE.json"):
        require_regular(root / evidence, f"Toolchain {evidence}")
    python = require_binary(root / safe_relative(str(manifest.get("python_path", ""))), "Private Python")
    uv = require_binary(root / safe_relative(str(manifest.get("uv_path", ""))), "Private uv")
    return PrivateToolchainV1(
        python=PinnedExecutable(
            path=python,
            sha256=sha256_file(python),
            version=str(manifest.get("python_version", "")),
        ),
        uv=PinnedExecutable(
            path=uv,
            sha256=sha256_file(uv),
            version=str(manifest.get("uv_version", "")),
        ),
    )


def run_plugin_verifier(runtime_manager: Path, manifest: Path, trust: Path, wheel: Path) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PATH"] = ""
    completed = subprocess.run(
        [str(runtime_manager), "verify-plugin-signature", str(manifest), str(trust), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ManagedPayloadError("Runtime Manager Plugin verification report is invalid")
    return value


def verify_assembled_release(release_dir: Path, release_digest: str, portable_manifest: dict[str, Any]) -> None:
    release_manifest = load_json(require_regular(release_dir / "manifest/release.json", "release manifest"))
    if release_manifest.get("release_digest") != release_digest:
        raise ManagedPayloadError("assembled release digest does not match manifest")
    receipt = load_json(require_regular(release_dir / "receipts/build-commands.json", "build receipt"))
    if not receipt:
        raise ManagedPayloadError("Managed Release build receipt is empty")
    signed = load_json(require_regular(release_dir / "plugin/metadata/signed-plugin-manifest.json", "published Plugin manifest"))
    if signed != portable_manifest:
        raise ManagedPayloadError("assembled release did not preserve portable Plugin manifest v2")
    for relative in ("host/venv", "connector/venv", "plugin/artifacts"):
        path = release_dir / relative
        if path.is_symlink() or not path.is_dir():
            raise ManagedPayloadError(f"assembled release is missing {relative}")


def artifact(path: Path) -> ArtifactInput:
    path = require_regular(path.resolve(), f"artifact {path.name}")
    return ArtifactInput(path=path, sha256=sha256_file(path))


def require_project(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ManagedPayloadError(f"{label} project root is invalid")
    for name in ("pyproject.toml", "uv.lock"):
        require_regular(path / name, f"{label} {name}")
    return path


def require_regular(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ManagedPayloadError(f"{label} must be an absolute regular non-symlink file")
    return path


def require_binary(path: Path, label: str) -> Path:
    path = require_regular(path, label)
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise ManagedPayloadError(f"{label} is not executable")
    return path


def copy_file(source: Path, destination: Path) -> None:
    source = require_regular(source.resolve(), f"source file {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(0o400)


def copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ManagedPayloadError(f"source tree is invalid: {source}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ManagedPayloadError(f"source tree contains symlink: {path}")
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)


def enumerate_files(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ManagedPayloadError(f"payload contains symlink: {path}")
        if not path.is_file():
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return files


def safe_relative(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManagedPayloadError(f"unsafe relative path: {value}")
    return path


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ManagedPayloadError(f"JSON file must contain an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o400)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ManagedPayloadError, OSError, RuntimeError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise SystemExit(f"managed_release_payload_error: {error}") from error
