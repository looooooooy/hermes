#!/usr/bin/env python3
"""Bind the trusted local Managed Release installer zipapp into an existing B1 payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    args = parser.parse_args()
    root = args.payload.resolve(strict=True)
    installer = args.installer.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("bootstrap payload root is invalid")
    if installer.is_symlink() or not installer.is_file() or installer.suffix != ".pyz":
        raise RuntimeError("managed release installer must be a regular .pyz file")
    manifest_path = root / "BOOTSTRAP-PAYLOAD.json"
    manifest = load_json(manifest_path)
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != {"desktop", "runtime_manager", "toolchain"}:
        raise RuntimeError("bootstrap payload is not an unaugmented B1 payload")

    destination = root / "runtime-manager" / "hermes-managed-release-installer.pyz"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("bootstrap update installer destination already exists")
    shutil.copy2(installer, destination)
    if os.name != "nt":
        destination.chmod(0o400)
    components["managed_release_installer"] = {
        "path": destination.relative_to(root).as_posix(),
        "sha256": sha256_file(destination),
        "size_bytes": destination.stat().st_size,
        "execution": "private_python_-I_zipapp",
        "network_install_allowed": False,
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
    manifest["update_installation"] = {
        "local_managed_release_installer_included": True,
        "requires_private_python": True,
        "requires_system_python": False,
        "requires_network_dependency_install": False,
    }
    write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "payload": str(root),
                "installer_sha256": components["managed_release_installer"]["sha256"],
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
