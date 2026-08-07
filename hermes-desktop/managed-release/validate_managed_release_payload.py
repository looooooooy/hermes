#!/usr/bin/env python3
"""Independently validate a DESKTOP-020B2 portable Managed Release payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "hermes-connector" / "packaging" / "common"
sys.path.insert(0, str(COMMON))
from hermes_offline_wheelhouse import load_verified_wheelhouse  # noqa: E402
from hermes_target_runtime_plan import load_verified_target_runtime_plan  # noqa: E402

TARGETS = {
    "macos-aarch64": ("macos", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "linux-x86_64": ("linux", "x86_64"),
    "windows-x86_64": ("windows", "x86_64"),
}
REQUIRED_PURPOSES = {
    "create-host-venv",
    "install-host-dependencies",
    "install-final-core-wheel",
    "verify-host-runtime",
    "create-connector-venv",
    "install-connector-dependencies",
    "install-final-connector-wheel",
    "verify-connector-runtime",
}


class ValidationError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--runtime-manager", type=Path, required=True)
    parser.add_argument("--expected-target", choices=sorted(TARGETS), required=True)
    args = parser.parse_args()

    root = args.payload.resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("Managed Release payload root is invalid")
    runtime_manager = args.runtime_manager.resolve()
    if runtime_manager.is_symlink() or not runtime_manager.is_file():
        raise ValidationError("Runtime Manager verifier is invalid")

    manifest = load_json(root / "MANAGED-RELEASE-PAYLOAD.json")
    platform_name, architecture = TARGETS[args.expected_target]
    require_equal(manifest.get("schema_version"), 1, "schema_version")
    require_equal(
        manifest.get("scope"), "hermes_managed_release_portable_inputs", "scope"
    )
    require_equal(
        manifest.get("publication_state"),
        "qualification-only-unsigned",
        "publication state",
    )
    require_equal(manifest.get("target"), args.expected_target, "target")
    require_equal(manifest.get("platform"), platform_name, "platform")
    require_equal(manifest.get("architecture"), architecture, "architecture")
    require_equal(
        manifest.get("final_local_assembly_required"), True, "local assembly policy"
    )
    require_equal(
        manifest.get("assembled_venv_included"), False, "venv shipping policy"
    )
    require_equal(
        manifest.get("required_next_gate"), "DESKTOP-020B3", "next gate"
    )

    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise ValidationError("payload file list is invalid")
    declared_map: dict[str, dict[str, Any]] = {}
    for item in declared:
        if not isinstance(item, dict):
            raise ValidationError("payload file entry is invalid")
        relative = str(item.get("path", ""))
        if not safe_relative(relative) or relative in declared_map:
            raise ValidationError(f"payload relative path is invalid: {relative}")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"declared payload file is missing: {relative}")
        require_equal(path.stat().st_size, item.get("size_bytes"), f"size {relative}")
        require_equal(sha256_file(path), item.get("sha256"), f"SHA {relative}")
        declared_map[relative] = item

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name != "MANAGED-RELEASE-PAYLOAD.json"
    }
    if actual != set(declared_map):
        raise ValidationError(
            f"payload file set mismatch; missing={sorted(set(declared_map)-actual)[:8]} "
            f"extra={sorted(actual-set(declared_map))[:8]}"
        )
    if any("/venv/" in f"/{relative}/" for relative in actual):
        raise ValidationError("portable B2 payload must not ship an assembled venv")

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
        "runtime_install_plan_sha256",
        "core_requirements_sha256",
        "connector_requirements_sha256",
        "portable_plugin_manifest_sha256",
        "plugin_trust_store_sha256",
        "files",
    )
    try:
        binding = {key: manifest[key] for key in binding_keys}
    except KeyError as exc:
        raise ValidationError("payload target runtime binding is incomplete") from exc
    expected_content_sha = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    require_equal(expected_content_sha, manifest.get("content_sha256"), "content SHA")

    wheelhouse_root = root / "wheelhouse"
    wheelhouse = load_verified_wheelhouse(wheelhouse_root)
    require_equal(wheelhouse.platform, platform_name, "wheelhouse platform")
    require_equal(wheelhouse.architecture, architecture, "wheelhouse architecture")
    require_equal(wheelhouse.python_tag, "cp313", "wheelhouse Python tag")
    require_equal(
        wheelhouse.locks.get("core"), manifest.get("core_lock_sha256"), "Core lock"
    )
    require_equal(
        wheelhouse.locks.get("connector"),
        manifest.get("connector_lock_sha256"),
        "Connector lock",
    )
    require_equal(
        wheelhouse.manifest_sha256,
        manifest.get("wheelhouse_manifest_sha256"),
        "wheelhouse manifest SHA",
    )
    if any(not artifact.filename.endswith(".whl") for artifact in wheelhouse.artifacts):
        raise ValidationError("wheelhouse contains a non-wheel artifact")

    runtime_plan = load_verified_target_runtime_plan(
        wheelhouse_root,
        expected_wheelhouse_manifest_sha256=wheelhouse.manifest_sha256,
    )
    require_equal(runtime_plan.target, args.expected_target, "runtime plan target")
    require_equal(runtime_plan.platform, platform_name, "runtime plan platform")
    require_equal(runtime_plan.architecture, architecture, "runtime plan architecture")
    require_equal(runtime_plan.python_tag, "cp313", "runtime plan Python tag")
    require_equal(
        runtime_plan.plan_sha256,
        manifest.get("runtime_install_plan_sha256"),
        "runtime plan SHA",
    )
    require_equal(
        runtime_plan.requirement("core").sha256,
        manifest.get("core_requirements_sha256"),
        "Core runtime requirements SHA",
    )
    require_equal(
        runtime_plan.requirement("connector").sha256,
        manifest.get("connector_requirements_sha256"),
        "Connector runtime requirements SHA",
    )

    portable_manifest = root / "plugin/portable-plugin-manifest.json"
    trust_store = root / "plugin/trust-store.json"
    plugin = load_json(portable_manifest)
    plugin_wheel = root / "plugin" / str(plugin.get("artifact_filename", ""))
    if plugin_wheel.is_symlink() or not plugin_wheel.is_file():
        raise ValidationError("portable Plugin wheel is missing")
    require_equal(
        sha256_file(portable_manifest),
        manifest.get("portable_plugin_manifest_sha256"),
        "Plugin manifest SHA",
    )
    require_equal(
        sha256_file(trust_store),
        manifest.get("plugin_trust_store_sha256"),
        "Plugin trust store SHA",
    )
    verification = run_plugin_verifier(
        runtime_manager, portable_manifest, trust_store, plugin_wheel
    )
    require_equal(
        verification.get("signature_verified"), True, "Plugin signature verification"
    )

    proof = load_json(root / "ASSEMBLY-PROOF.json")
    require_equal(
        proof.get("scope"), "managed_release_offline_assembly_proof", "proof scope"
    )
    require_equal(proof.get("target"), args.expected_target, "proof target")
    require_equal(
        proof.get("release_id"), manifest.get("release_id"), "proof release_id"
    )
    require_equal(
        proof.get("portable_plugin_signature_verified"), True, "proof Plugin signature"
    )
    require_equal(proof.get("wheelhouse_binary_only"), True, "proof binary wheelhouse")
    require_equal(
        proof.get("target_runtime_plan_verified"), True, "proof target runtime plan"
    )
    require_equal(
        proof.get("runtime_plan_sha256"), runtime_plan.plan_sha256, "proof runtime plan SHA"
    )
    require_equal(proof.get("private_toolchain_used"), True, "proof private toolchain")
    require_equal(
        proof.get("network_dependency_install_allowed"), False, "proof network policy"
    )
    require_equal(
        proof.get("assembled_release_shipped"), False, "proof venv shipping policy"
    )
    purposes = proof.get("command_purposes")
    if not isinstance(purposes, list) or set(purposes) != REQUIRED_PURPOSES:
        raise ValidationError(f"proof command purposes are invalid: {purposes}")

    print(
        json.dumps(
            {
                "schema_version": 1,
                "target": args.expected_target,
                "release_id": manifest["release_id"],
                "release_digest": proof["release_digest"],
                "content_sha256": manifest["content_sha256"],
                "wheel_count": len(wheelhouse.artifacts),
                "target_runtime_plan_verified": True,
                "portable_plugin_signature_verified": True,
                "managed_release_payload_verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def run_plugin_verifier(
    runtime_manager: Path, manifest: Path, trust: Path, wheel: Path
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PATH"] = ""
    completed = subprocess.run(
        [
            str(runtime_manager),
            "verify-plugin-signature",
            str(manifest),
            str(trust),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValidationError("Runtime Manager verifier output is invalid")
    return value


def safe_relative(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
    )


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"JSON input is invalid: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError(f"JSON input must be an object: {path}")
    return value


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ValidationError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ValidationError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        raise SystemExit(f"managed_release_payload_validation_error: {error}") from error
