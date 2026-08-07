#!/usr/bin/env python3
"""Build the DESKTOP-020B2 closed runtime dependency wheelhouse.

This runs only in qualification CI. Customer machines consume the resulting wheelhouse
strictly offline through the pinned Hermes Private uv; they never invoke pip/download.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "hermes-connector" / "packaging" / "common"
sys.path.insert(0, str(COMMON))

from hermes_offline_wheelhouse import load_verified_wheelhouse  # noqa: E402

TARGETS = {
    "macos-aarch64": ("macos", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "linux-x86_64": ("linux", "x86_64"),
    "windows-x86_64": ("windows", "x86_64"),
}


class WheelhouseBuildError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--core-project", type=Path, required=True)
    parser.add_argument("--connector-project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    platform_name, architecture = TARGETS[args.target]
    uv = require_executable(args.uv.resolve(), "Private uv")
    core = require_project(args.core_project.resolve(), "Core")
    connector = require_project(args.connector_project.resolve(), "Connector")
    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        raise WheelhouseBuildError(f"wheelhouse output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    core_lock = sha256_file(core / "uv.lock")
    connector_lock = sha256_file(connector / "uv.lock")
    with tempfile.TemporaryDirectory(prefix="hermes-wheelhouse-") as temporary:
        scratch = Path(temporary)
        requirements = []
        for label, project in (("core", core), ("connector", connector)):
            exported = subprocess.run(
                [
                    str(uv),
                    "export",
                    "--project",
                    str(project),
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
                raise WheelhouseBuildError(f"{label} lock export produced no requirements")
            path = scratch / f"{label}-requirements.txt"
            path.write_text(exported, encoding="utf-8")
            requirements.append(path)

        stage = scratch / "wheelhouse"
        stage.mkdir(mode=0o700)
        for requirement in requirements:
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
                    str(stage),
                    "--requirement",
                    str(requirement),
                ],
                check=True,
                timeout=300,
            )

        artifacts = []
        for path in sorted(stage.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
                raise WheelhouseBuildError(f"non-wheel dependency artifact is forbidden: {path.name}")
            artifacts.append(
                {
                    "filename": path.name,
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        if not artifacts:
            raise WheelhouseBuildError("runtime wheelhouse is empty")
        manifest = {
            "schema_version": 1,
            "platform": platform_name,
            "architecture": architecture,
            "python_tag": "cp313",
            "locks": {"core": core_lock, "connector": connector_lock},
            "artifacts": artifacts,
        }
        (stage / "WHEELHOUSE-MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            for path in stage.iterdir():
                path.chmod(0o400)
        load_verified_wheelhouse(stage)
        shutil.copytree(stage, output, symlinks=False, copy_function=shutil.copy2)

    verified = load_verified_wheelhouse(output)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "target": args.target,
                "platform": verified.platform,
                "architecture": verified.architecture,
                "python_tag": verified.python_tag,
                "core_lock_sha256": core_lock,
                "connector_lock_sha256": connector_lock,
                "wheel_count": len(verified.artifacts),
                "wheelhouse_root": str(output),
                "binary_only": True,
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def require_executable(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise WheelhouseBuildError(f"{label} must be an absolute regular non-symlink file")
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise WheelhouseBuildError(f"{label} is not executable")
    return path


def require_project(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise WheelhouseBuildError(f"{label} project root is invalid")
    for filename in ("pyproject.toml", "uv.lock"):
        candidate = path / filename
        if candidate.is_symlink() or not candidate.is_file():
            raise WheelhouseBuildError(f"{label} project is missing {filename}")
    return path


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
    except (WheelhouseBuildError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise SystemExit(f"runtime_wheelhouse_error: {error}") from error
