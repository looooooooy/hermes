"""Virtual-environment installation and runtime validation."""

from __future__ import annotations

import json
import subprocess
import sys
import venv
from collections.abc import Iterable
from pathlib import Path

from .models import (
    CANONICAL_DISTRIBUTION,
    LEGACY_DISTRIBUTION,
    TARGET_ENTRY_POINT_NAMES,
    UpgradeTransactionError,
)


def _python_path(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts/python.exe"
    return environment / "bin/python"


def _run(
    environment: Path,
    arguments: list[str],
) -> subprocess.CompletedProcess:
    python = _python_path(environment)
    try:
        return subprocess.run(
            [str(python), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise UpgradeTransactionError(
            f"environment command failed with exit code {error.returncode}"
        ) from error


def create_environment(environment: Path) -> None:
    """Create a venv compatible with uv-managed macOS interpreters."""
    venv.EnvBuilder(
        with_pip=True,
        symlinks=sys.platform != "win32",
    ).create(environment)


def install_wheel(
    environment: Path,
    wheel: Path,
    *,
    force_reinstall: bool = False,
) -> None:
    """Install an already trusted Bundle wheel without resolving dependencies."""
    arguments = [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
    ]
    if force_reinstall:
        arguments.append("--force-reinstall")
    arguments.append(str(wheel))
    _run(environment, arguments)


def install_bundle_wheels(
    environment: Path,
    wheels: Iterable[Path],
) -> None:
    """Install already trusted and locked Bundle wheels without resolution."""
    for wheel in wheels:
        install_wheel(environment, wheel)


def uninstall_distribution(environment: Path, distribution: str) -> None:
    """Uninstall one distribution from the stopped extension environment."""
    _run(
        environment,
        [
            "-m",
            "pip",
            "uninstall",
            "--disable-pip-version-check",
            "-y",
            distribution,
        ],
    )


def inspect_environment(environment: Path) -> dict:
    """Return installed target versions, entry points, and runtime imports."""
    inspection = _run(
        environment,
        [
            "-c",
            (
                "import importlib, importlib.metadata as m, json;"
                "\n"
                "def version(name):"
                "\n try:return m.version(name)"
                "\n except m.PackageNotFoundError:return None"
                "\ncanonical=version('hermes-agent-plugin');"
                "\nlegacy=version('hermes-mobile-gateway');"
                "\neps=sorted([{'name':ep.name,'value':ep.value,"
                "'distribution':ep.dist.metadata['Name'],"
                "'version':ep.dist.version} for ep in m.entry_points("
                "group='hermes_agent.plugins')],"
                "key=lambda ep:(ep['name'],ep['distribution'],ep['value']));"
                "\ncritical={};"
                "\nfor module_name in ('websockets',):"
                "\n try:importlib.import_module(module_name);"
                "critical[module_name]=True"
                "\n except Exception:critical[module_name]=False"
                "\ntry:importlib.import_module('hermes_agent_plugin');"
                "canonical_import=True"
                "\nexcept Exception:canonical_import=False"
                "\ntry:importlib.import_module('hermes_mobile_gateway');"
                "legacy_import=True"
                "\nexcept Exception:legacy_import=False"
                "\nprint(json.dumps({"
                "'canonical':canonical,"
                "'legacy':legacy,"
                "'entry_points':eps,"
                "'canonical_import':canonical_import,"
                "'legacy_import':legacy_import,"
                "'critical_imports':critical"
                "}))"
            ),
        ],
    )
    return json.loads(inspection.stdout)


def _target_entry_points(inspection: dict) -> list[dict]:
    return [
        entry_point
        for entry_point in inspection["entry_points"]
        if entry_point["name"] in TARGET_ENTRY_POINT_NAMES
    ]


def pip_check(environment: Path) -> None:
    """Require the preinstalled locked Bundle to satisfy all requirements."""
    _run(environment, ["-m", "pip", "check"])


def validate_legacy(environment: Path, expected_version: str) -> None:
    """Validate old identity, ownership, dependencies, and key imports."""
    pip_check(environment)
    inspection = inspect_environment(environment)
    expected_entry_points = [
        {
            "name": LEGACY_DISTRIBUTION,
            "value": "hermes_mobile_gateway",
            "distribution": LEGACY_DISTRIBUTION,
            "version": expected_version,
        }
    ]
    if (
        inspection["legacy"] != expected_version
        or inspection["canonical"] is not None
        or _target_entry_points(inspection) != expected_entry_points
        or inspection["canonical_import"]
        or not inspection["legacy_import"]
        or inspection["critical_imports"] != {"websockets": True}
    ):
        raise UpgradeTransactionError("legacy distribution validation failed")


def validate_canonical(environment: Path, expected_version: str) -> None:
    """Validate new identity, ownership, dependencies, and key imports."""
    pip_check(environment)
    inspection = inspect_environment(environment)
    expected_entry_points = [
        {
            "name": CANONICAL_DISTRIBUTION,
            "value": "hermes_agent_plugin",
            "distribution": CANONICAL_DISTRIBUTION,
            "version": expected_version,
        }
    ]
    if (
        inspection["canonical"] != expected_version
        or inspection["legacy"] is not None
        or _target_entry_points(inspection) != expected_entry_points
        or not inspection["canonical_import"]
        or inspection["legacy_import"]
        or inspection["critical_imports"] != {"websockets": True}
    ):
        raise UpgradeTransactionError("canonical distribution validation failed")
