#!/usr/bin/env python3
"""Install one verified portable Managed Release payload into an immutable local release.

This program is intended to run from the Hermes-managed installer zipapp with Private
CPython (`python -I installer.pyz`). It never downloads dependencies and never imports
customer/site Python packages. The caller must already have verified the outer release
artifact SHA-256 and signed Product Release identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from hermes_local_release import ArtifactInput, ReleaseInputs, RuntimeReleaseInput
from hermes_managed_release import ManagedReleaseAssembler
from hermes_offline_wheelhouse import load_verified_wheelhouse
from hermes_private_toolchain import PinnedExecutable, PrivateToolchainV1

MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class LocalAssemblyError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--runtime-manager", type=Path, required=True)
    parser.add_argument("--qualified-toolchain", type=Path, required=True)
    parser.add_argument("--releases-root", type=Path, required=True)
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--expected-target", required=True)
    args = parser.parse_args()

    payload = canonical_dir(args.payload, "payload")
    runtime_manager = executable(args.runtime_manager, "Runtime Manager")
    toolchain_root = canonical_dir(args.qualified_toolchain, "Private Toolchain")
    releases_root = canonical_root(args.releases_root, "releases root")

    manifest = load_json(payload / "MANAGED-RELEASE-PAYLOAD.json")
    validate_payload_manifest(
        payload,
        manifest,
        expected_release_id=args.expected_release_id,
        expected_target=args.expected_target,
    )
    toolchain = load_toolchain(toolchain_root)
    wheelhouse = load_verified_wheelhouse(payload / "wheelhouse")
    if wheelhouse.python_tag != "cp313":
        raise LocalAssemblyError("Managed Release wheelhouse must target cp313")

    core_project = canonical_dir(payload / "core/project", "Core project")
    connector_project = canonical_dir(payload / "connector/project", "Connector project")
    core_wheel = exactly_one_wheel(payload / "core/dist", "Core")
    connector_wheel = exactly_one_wheel(payload / "connector/dist", "Connector")
    plugin_manifest_path = regular(payload / "plugin/portable-plugin-manifest.json", "Plugin manifest")
    plugin_trust_path = regular(payload / "plugin/trust-store.json", "Plugin trust store")
    plugin_manifest = load_json(plugin_manifest_path)
    plugin_wheel = regular(
        payload / "plugin" / safe_filename(plugin_manifest.get("artifact_filename"), "Plugin wheel"),
        "Plugin wheel",
    )

    verify_plugin(runtime_manager, plugin_manifest_path, plugin_trust_path, plugin_wheel)
    inputs = ReleaseInputs(
        release_id=args.expected_release_id,
        core=RuntimeReleaseInput(
            project_name="hermes-agent",
            version=str(manifest.get("core_version", "")),
            wheel=artifact(core_wheel),
            lock=artifact(regular(core_project / "uv.lock", "Core uv.lock")),
            project=artifact(regular(core_project / "pyproject.toml", "Core pyproject")),
            console_script="hermes",
            entrypoint="hermes_cli.main:main",
            launch_module="hermes_cli.main",
        ),
        plugin_bundle=artifact(plugin_wheel),
        plugin_store_manifest=artifact(plugin_trust_path),
        signed_plugin_manifest=plugin_manifest,
        connector=RuntimeReleaseInput(
            project_name="hermes-connector",
            version=str(manifest.get("connector_version", "")),
            wheel=artifact(connector_wheel),
            lock=artifact(regular(connector_project / "uv.lock", "Connector uv.lock")),
            project=artifact(regular(connector_project / "pyproject.toml", "Connector pyproject")),
            console_script="hermes-connector",
            entrypoint="hermes_connector.cli:main",
            launch_module="hermes_connector.cli",
        ),
    )
    require_equal(wheelhouse.locks.get("core"), inputs.core.lock.sha256, "Core lock")
    require_equal(wheelhouse.locks.get("connector"), inputs.connector.lock.sha256, "Connector lock")

    def portable_verifier(_inputs: ReleaseInputs) -> None:
        verify_plugin(runtime_manager, plugin_manifest_path, plugin_trust_path, plugin_wheel)

    published = ManagedReleaseAssembler(
        releases_root=releases_root,
        toolchain=toolchain,
        wheelhouse=wheelhouse,
        portable_plugin_verifier=portable_verifier,
    ).build(inputs)
    release_dir = published.release_dir.resolve(strict=True)
    if release_dir.parent != releases_root.resolve(strict=True) or release_dir.name != args.expected_release_id:
        raise LocalAssemblyError("assembled release escaped the exact immutable release root")
    release_manifest = load_json(release_dir / "manifest/release.json")
    require_equal(release_manifest.get("release_id"), args.expected_release_id, "assembled release_id")
    require_equal(release_manifest.get("release_digest"), published.release_digest, "release digest")
    for relative in ("host/venv", "connector/venv", "plugin/artifacts"):
        canonical_dir(release_dir / relative, relative)

    print(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": args.expected_release_id,
                "target": args.expected_target,
                "release_path": str(release_dir),
                "release_digest": published.release_digest,
                "content_verified": True,
                "private_toolchain_used": True,
                "network_dependency_install_allowed": False,
                "reused_existing": bool(getattr(published, "reused", False)),
            },
            sort_keys=True,
        )
    )
    return 0


def validate_payload_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    expected_release_id: str,
    expected_target: str,
) -> None:
    require_equal(manifest.get("schema_version"), 1, "payload schema")
    require_equal(manifest.get("scope"), "hermes_managed_release_portable_inputs", "payload scope")
    require_equal(manifest.get("release_id"), expected_release_id, "payload release_id")
    require_equal(manifest.get("target"), expected_target, "payload target")
    require_equal(manifest.get("final_local_assembly_required"), True, "local assembly policy")
    require_equal(manifest.get("assembled_venv_included"), False, "venv shipping policy")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise LocalAssemblyError("payload file manifest is invalid")
    declared: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise LocalAssemblyError("payload file entry is invalid")
        relative = safe_relative(item.get("path"), "payload file")
        if relative in declared:
            raise LocalAssemblyError("payload file entry is duplicated")
        declared.add(relative)
        path = regular(root / relative, relative)
        require_equal(path.stat().st_size, item.get("size_bytes"), f"size {relative}")
        require_equal(sha256_file(path), item.get("sha256"), f"SHA {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "MANAGED-RELEASE-PAYLOAD.json"
    }
    if actual != declared:
        raise LocalAssemblyError("payload file set does not match signed portable manifest")
    if any("/venv/" in f"/{name}/" for name in actual):
        raise LocalAssemblyError("portable payload must not contain prebuilt venvs")
    binding_keys = (
        "schema_version",
        "target",
        "platform",
        "architecture",
        "release_id",
        "core_version",
        "plugin_version",
        "connector_version",
        "core_lock_sha256",
        "connector_lock_sha256",
        "wheelhouse_manifest_sha256",
        "portable_plugin_manifest_sha256",
        "plugin_trust_store_sha256",
        "files",
    )
    binding = {key: manifest[key] for key in binding_keys}
    require_equal(
        hashlib.sha256(canonical_json(binding)).hexdigest(),
        manifest.get("content_sha256"),
        "payload content SHA",
    )


def load_toolchain(root: Path) -> PrivateToolchainV1:
    manifest = load_json(root / "TOOLCHAIN-BUNDLE.json")
    require_equal(manifest.get("schema_version"), 1, "Toolchain schema")
    regular(root / "LICENSE-EVIDENCE.json", "Toolchain license evidence")
    regular(root / "UPSTREAM-SOURCE.json", "Toolchain source evidence")
    python = executable(root / safe_relative(manifest.get("python_path"), "Private Python path"), "Private Python")
    uv = executable(root / safe_relative(manifest.get("uv_path"), "Private uv path"), "Private uv")
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


def verify_plugin(runtime_manager: Path, manifest: Path, trust: Path, wheel: Path) -> None:
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
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LocalAssemblyError("Runtime Manager Plugin verifier returned invalid JSON") from exc
    if not isinstance(report, dict) or report.get("signature_verified") is not True:
        raise LocalAssemblyError("portable Plugin vendor signature verification failed")


def exactly_one_wheel(root: Path, label: str) -> Path:
    root = canonical_dir(root, f"{label} dist")
    wheels = [path for path in root.iterdir() if path.is_file() and not path.is_symlink() and path.suffix == ".whl"]
    if len(wheels) != 1:
        raise LocalAssemblyError(f"{label} dist must contain exactly one wheel")
    return wheels[0].resolve(strict=True)


def artifact(path: Path) -> ArtifactInput:
    path = regular(path, path.name)
    return ArtifactInput(path=path, sha256=sha256_file(path))


def canonical_root(path: Path, label: str) -> Path:
    path = path.resolve(strict=False)
    if path.is_symlink():
        raise LocalAssemblyError(f"{label} must not be symlinked")
    if path.exists() and not path.is_dir():
        raise LocalAssemblyError(f"{label} must be a directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def canonical_dir(path: Path, label: str) -> Path:
    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_dir():
        raise LocalAssemblyError(f"{label} is not a canonical directory")
    return path


def regular(path: Path, label: str) -> Path:
    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise LocalAssemblyError(f"{label} is not a canonical regular file")
    return path


def executable(path: Path, label: str) -> Path:
    path = regular(path, label)
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise LocalAssemblyError(f"{label} is not executable")
    return path


def safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LocalAssemblyError(f"{label} is invalid")
    path = Path(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise LocalAssemblyError(f"{label} escapes the payload root")
    return path.as_posix()


def safe_filename(value: object, label: str) -> str:
    relative = safe_relative(value, label)
    if "/" in relative or "\\" in relative:
        raise LocalAssemblyError(f"{label} must be a filename")
    return relative


def load_json(path: Path) -> dict[str, Any]:
    path = regular(path, path.name)
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise LocalAssemblyError(f"JSON input is empty or oversized: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalAssemblyError(f"JSON input is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise LocalAssemblyError(f"JSON input is not an object: {path.name}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise LocalAssemblyError(f"{label} mismatch")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LocalAssemblyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"managed_release_local_install_error: {exc}") from exc
