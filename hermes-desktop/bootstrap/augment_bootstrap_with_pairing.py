#!/usr/bin/env python3
"""Bind a verified pairing-only Connector artifact into an existing B1 payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

_SUPPORTED_PLATFORMS = {"macos", "windows"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--pairing", type=Path, required=True)
    args = parser.parse_args()

    root = args.payload.resolve(strict=True)
    pairing = args.pairing.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("bootstrap payload root is invalid")
    if pairing.is_symlink() or not pairing.is_dir():
        raise RuntimeError("pairing bootstrap root is invalid")

    manifest_path = root / "BOOTSTRAP-PAYLOAD.json"
    manifest = load_json(manifest_path)
    platform_name = manifest.get("platform")
    if platform_name not in _SUPPORTED_PLATFORMS:
        raise RuntimeError("pairing bootstrap is unsupported for this platform")
    components = manifest.get("components")
    expected = {"desktop", "runtime_manager", "toolchain", "managed_release_installer"}
    if not isinstance(components, dict) or set(components) != expected:
        raise RuntimeError("bootstrap payload is not ready for pairing augmentation")

    pairing_manifest = load_json(pairing / "PAIRING-BOOTSTRAP.json")
    if pairing_manifest.get("target") != manifest.get("target"):
        raise RuntimeError("pairing bootstrap target mismatch")
    if pairing_manifest.get("platform") != platform_name:
        raise RuntimeError("pairing bootstrap platform mismatch")
    if pairing_manifest.get("network_dependency_install") is not False:
        raise RuntimeError("pairing bootstrap network policy is invalid")

    destination = root / "pairing"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("pairing bootstrap destination already exists")
    shutil.copytree(pairing, destination, symlinks=False, copy_function=shutil.copy2)
    copied_manifest = destination / "PAIRING-BOOTSTRAP.json"
    components["pairing_bootstrap"] = {
        "root": "pairing",
        "manifest_path": "pairing/PAIRING-BOOTSTRAP.json",
        "manifest_sha256": sha256_file(copied_manifest),
        "connector_version": pairing_manifest["connector_version"],
        "connector_lock_sha256": pairing_manifest["connector_lock_sha256"],
        "entrypoint_module": "hermes_connector.cli",
        "allowed_actions": ["pair start", "pair status", "pair cancel"],
        "requires_private_python": True,
        "requires_system_python": False,
        "network_dependency_install": False,
    }
    binding = {
        "schema_version": manifest["schema_version"],
        "target": manifest["target"],
        "platform": manifest["platform"],
        "architecture": manifest["architecture"],
        "components": components,
    }
    manifest["components"] = components
    manifest["content_sha256"] = hashlib.sha256(canonical_json(binding)).hexdigest()
    manifest["pairing_bootstrap"] = {
        "included": True,
        "credential_authority": pairing_manifest["credential_authority"],
        "state_authority": "Hermes Connector pairing v1",
        "managed_runtime_required": False,
        "network_dependency_install": False,
    }
    write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "payload": str(root),
                "pairing_manifest_sha256": components["pairing_bootstrap"]["manifest_sha256"],
                "content_sha256": manifest["content_sha256"],
                "augmented": True,
            },
            sort_keys=True,
        )
    )
    return 0


def load_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("bootstrap manifest is invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("bootstrap manifest is not an object")
    return value


def write_json_atomic(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.new")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o400)
    os.replace(temporary, path)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
