#!/usr/bin/env python3
"""Build the B1 pairing-only Connector artifact for macOS and Windows.

Qualification CI may access package indexes. Customer machines consume this artifact
strictly offline through Hermes Private Python/uv and never depend on host Python/pip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

TARGETS = {
    "macos-aarch64": ("macos", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "windows-x86_64": ("windows", "x86_64"),
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WHEEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\.whl\Z")


class PairingBootstrapError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--connector-project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    platform_name, architecture = TARGETS[args.target]
    uv = require_executable(args.uv.resolve(), "Private uv")
    connector = require_project(args.connector_project.resolve())
    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        raise PairingBootstrapError(f"pairing bootstrap output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    connector_lock_sha256 = sha256_file(connector / "uv.lock")
    connector_version = read_connector_version(connector / "pyproject.toml")

    with tempfile.TemporaryDirectory(prefix="hermes-pairing-bootstrap-") as temporary:
        scratch = Path(temporary)
        requirements = scratch / "requirements.txt"
        exported = subprocess.run(
            [
                str(uv),
                "export",
                "--project",
                str(connector),
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
            env=sanitized_environment(),
        ).stdout
        if not exported.strip():
            raise PairingBootstrapError("Connector lock export produced no requirements")
        requirements.write_text(exported, encoding="utf-8")

        stage = scratch / "pairing"
        wheels = stage / "wheels"
        wheels.mkdir(parents=True, mode=0o700)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--require-hashes",
                "--only-binary=:all:",
                "--dest",
                str(wheels),
                "--requirement",
                str(requirements),
            ],
            check=True,
            timeout=300,
        )
        subprocess.run(
            [
                str(uv),
                "build",
                "--wheel",
                "--project",
                str(connector),
                "--out-dir",
                str(wheels),
            ],
            check=True,
            timeout=180,
            env=sanitized_environment(),
        )

        artifacts = wheel_artifacts(wheels)
        connector_wheels = [
            item for item in artifacts if item["filename"].startswith("hermes_connector-")
        ]
        if len(connector_wheels) != 1:
            raise PairingBootstrapError("pairing bootstrap must contain exactly one Connector wheel")
        manifest = {
            "schema_version": 1,
            "scope": "hermes_desktop_pairing_bootstrap",
            "target": args.target,
            "platform": platform_name,
            "architecture": architecture,
            "python_tag": "cp313",
            "connector_version": connector_version,
            "connector_lock_sha256": connector_lock_sha256,
            "connector_wheel": connector_wheels[0]["filename"],
            "entrypoint_module": "hermes_connector.cli",
            "allowed_actions": ["pair start", "pair status", "pair cancel"],
            "credential_authority": (
                "macos-keychain" if platform_name == "macos" else "windows-dpapi"
            ),
            "network_dependency_install": False,
            "artifacts": artifacts,
        }
        manifest_path = stage / "PAIRING-BOOTSTRAP.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        validate_stage(stage, manifest)
        if os.name != "nt":
            manifest_path.chmod(0o400)
            for path in wheels.iterdir():
                path.chmod(0o400)
        shutil.copytree(stage, output, symlinks=False, copy_function=shutil.copy2)

    print(
        json.dumps(
            {
                "pairing_bootstrap": str(output),
                "target": args.target,
                "connector_version": connector_version,
                "connector_lock_sha256": connector_lock_sha256,
                "wheel_count": len(artifacts),
                "offline_customer_install": True,
                "assembled": True,
            },
            sort_keys=True,
        )
    )
    return 0


def require_executable(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PairingBootstrapError(f"{label} must be an absolute regular non-symlink file")
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise PairingBootstrapError(f"{label} is not executable")
    return path


def require_project(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise PairingBootstrapError("Connector project root is invalid")
    for filename in ("pyproject.toml", "uv.lock"):
        candidate = path / filename
        if candidate.is_symlink() or not candidate.is_file():
            raise PairingBootstrapError(f"Connector project is missing {filename}")
    return path


def read_connector_version(path: Path) -> str:
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    version = value.get("project", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise PairingBootstrapError("Connector version is invalid")
    return version


def wheel_artifacts(root: Path) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    names: set[str] = set()
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or _WHEEL.fullmatch(path.name) is None:
            raise PairingBootstrapError(f"non-wheel pairing artifact is forbidden: {path.name}")
        if path.name in names:
            raise PairingBootstrapError("duplicate pairing wheel artifact")
        names.add(path.name)
        artifacts.append(
            {
                "filename": path.name,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    if not artifacts:
        raise PairingBootstrapError("pairing bootstrap wheelhouse is empty")
    return artifacts


def validate_stage(root: Path, manifest: dict[str, object]) -> None:
    if manifest.get("schema_version") != 1 or manifest.get("network_dependency_install") is not False:
        raise PairingBootstrapError("pairing bootstrap manifest policy is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PairingBootstrapError("pairing bootstrap manifest has no artifacts")
    declared: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise PairingBootstrapError("pairing bootstrap artifact entry is invalid")
        filename = item.get("filename")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if not isinstance(filename, str) or _WHEEL.fullmatch(filename) is None:
            raise PairingBootstrapError("pairing bootstrap artifact filename is invalid")
        if filename in declared:
            raise PairingBootstrapError("pairing bootstrap artifact is duplicated")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise PairingBootstrapError("pairing bootstrap artifact digest is invalid")
        path = root / "wheels" / filename
        if path.is_symlink() or not path.is_file():
            raise PairingBootstrapError("pairing bootstrap artifact is missing")
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise PairingBootstrapError("pairing bootstrap artifact verification failed")
        declared.add(filename)
    actual = {path.name for path in (root / "wheels").iterdir()}
    if actual != declared:
        raise PairingBootstrapError("pairing bootstrap wheel set is not closed")


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(key, None)
    environment["UV_NO_PROGRESS"] = "1"
    environment["UV_NO_SYSTEM_CONFIG"] = "1"
    return environment


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PairingBootstrapError, OSError, subprocess.SubprocessError, ValueError) as error:
        raise SystemExit(f"pairing_bootstrap_error: {error}") from error
