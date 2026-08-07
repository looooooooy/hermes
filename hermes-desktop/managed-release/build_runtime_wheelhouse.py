#!/usr/bin/env python3
"""Build the DESKTOP-020B2 closed runtime dependency wheelhouse.

Qualification CI derives one target-specific, hash-bound install plan from the same
Core/Connector uv locks used for the release. Customer machines consume the exported
requirements strictly offline through Hermes Private uv; they never re-solve the
universal lock or invoke network package discovery.
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
from hermes_target_runtime_plan import load_verified_target_runtime_plan  # noqa: E402

TARGETS = {
    "macos-aarch64": ("macos", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "linux-x86_64": ("linux", "x86_64"),
    "windows-x86_64": ("windows", "x86_64"),
}
_REQUIREMENT_FILES = {
    "core": "CORE-RUNTIME-REQUIREMENTS.txt",
    "connector": "CONNECTOR-RUNTIME-REQUIREMENTS.txt",
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

    locks = {
        "core": sha256_file(core / "uv.lock"),
        "connector": sha256_file(connector / "uv.lock"),
    }
    with tempfile.TemporaryDirectory(prefix="hermes-wheelhouse-") as temporary:
        scratch = Path(temporary)
        requirements: dict[str, Path] = {}
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
            if not exported.strip() or "--hash=" not in exported:
                raise WheelhouseBuildError(
                    f"{label} lock export did not produce hash-bound requirements"
                )
            path = scratch / f"{label}-requirements.txt"
            path.write_text(exported, encoding="utf-8")
            requirements[label] = path

        stage = scratch / "wheelhouse"
        stage.mkdir(mode=0o700)
        for requirement in requirements.values():
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
                raise WheelhouseBuildError(
                    f"non-wheel dependency artifact is forbidden: {path.name}"
                )
            artifacts.append(
                {
                    "filename": path.name,
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        if not artifacts:
            raise WheelhouseBuildError("runtime wheelhouse is empty")

        wheelhouse_manifest = {
            "schema_version": 1,
            "platform": platform_name,
            "architecture": architecture,
            "python_tag": "cp313",
            "locks": locks,
            "artifacts": artifacts,
        }
        wheelhouse_manifest_path = stage / "WHEELHOUSE-MANIFEST.json"
        wheelhouse_manifest_path.write_text(
            json.dumps(wheelhouse_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        wheelhouse_manifest_sha = sha256_file(wheelhouse_manifest_path)

        requirement_manifest: dict[str, dict[str, object]] = {}
        for label, source in requirements.items():
            destination = stage / _REQUIREMENT_FILES[label]
            shutil.copy2(source, destination)
            requirement_manifest[label] = {
                "filename": destination.name,
                "sha256": sha256_file(destination),
                "size_bytes": destination.stat().st_size,
            }

        runtime_plan = {
            "schema_version": 1,
            "target": args.target,
            "platform": platform_name,
            "architecture": architecture,
            "python_tag": "cp313",
            "wheelhouse_manifest_sha256": wheelhouse_manifest_sha,
            "locks": locks,
            "requirements": requirement_manifest,
        }
        (stage / "RUNTIME-INSTALL-PLAN.json").write_text(
            json.dumps(runtime_plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if os.name != "nt":
            for path in stage.iterdir():
                if path.is_file():
                    path.chmod(0o400)
        load_verified_wheelhouse(stage)
        load_verified_target_runtime_plan(
            stage,
            expected_wheelhouse_manifest_sha256=wheelhouse_manifest_sha,
        )
        shutil.copytree(stage, output, symlinks=False, copy_function=shutil.copy2)

    verified = load_verified_wheelhouse(output)
    runtime_plan = load_verified_target_runtime_plan(
        output,
        expected_wheelhouse_manifest_sha256=verified.manifest_sha256,
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "target": args.target,
                "platform": verified.platform,
                "architecture": verified.architecture,
                "python_tag": verified.python_tag,
                "core_lock_sha256": locks["core"],
                "connector_lock_sha256": locks["connector"],
                "core_requirements_sha256": runtime_plan.requirement("core").sha256,
                "connector_requirements_sha256": runtime_plan.requirement("connector").sha256,
                "runtime_plan_sha256": runtime_plan.plan_sha256,
                "wheel_count": len(verified.artifacts),
                "wheelhouse_root": str(output),
                "binary_only": True,
                "target_install_plan_verified": True,
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def require_executable(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise WheelhouseBuildError(
            f"{label} must be an absolute regular non-symlink file"
        )
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
    except (
        WheelhouseBuildError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        raise SystemExit(f"runtime_wheelhouse_error: {error}") from error
