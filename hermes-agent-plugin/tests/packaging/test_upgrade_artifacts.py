"""Persistent wheel cache, receipt, and restart rollback tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _initialize(
    upgrade_internals,
    environment: Path,
    legacy_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
) -> None:
    upgrade_internals.initialize_legacy_environment(
        environment,
        legacy_wheel,
        bundle_wheels=runtime_dependency_wheels,
    )


def _target_entry_points(inspection: dict) -> list[dict]:
    return [
        entry_point
        for entry_point in inspection["entry_points"]
        if entry_point["name"] in {"hermes-mobile-gateway", "hermes-agent-plugin"}
    ]


def test_wrong_legacy_wheel_is_rejected_before_uninstall(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    legacy_wheel_factory: Callable[[str], Path],
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
) -> None:
    environment = tmp_path / "extension-environment"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    wrong_legacy_wheel = legacy_wheel_factory("0.0.8")
    transaction_directory = tmp_path / "transaction"

    with pytest.raises(upgrade_module.UpgradeTransactionError):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=wrong_legacy_wheel,
            transaction_directory=transaction_directory,
            host_stopped=True,
        )

    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["legacy"] == "0.0.9"
    assert inspection["canonical"] is None
    assert not transaction_directory.exists()


def test_stopped_upgrade_persists_cached_artifacts_before_uninstall(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
) -> None:
    environment = tmp_path / "extension-environment"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    transaction_directory = tmp_path / "transaction"

    receipt = upgrade_module.upgrade_legacy_distribution(
        environment,
        canonical_wheel=canonical_wheel,
        legacy_wheel=legacy_wheel,
        transaction_directory=transaction_directory,
        host_stopped=True,
    )

    assert receipt.status == "completed"
    assert receipt.receipt_path.is_file()
    for artifact in (receipt.legacy_artifact, receipt.canonical_artifact):
        assert artifact.path.parent == transaction_directory
        assert artifact.path.is_file()
        assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
        assert len(artifact.sha256) == 64
    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["canonical"] == "0.1.0"
    assert inspection["legacy"] is None
    assert _target_entry_points(inspection) == [
        {
            "name": "hermes-agent-plugin",
            "value": "hermes_agent_plugin",
            "distribution": "hermes-agent-plugin",
            "version": "0.1.0",
        }
    ]
    upgrade_internals.uninstall_distribution(
        environment,
        "hermes-mobile-gateway",
    )
    assert upgrade_module.inspect_environment(environment)["canonical"] == ("0.1.0")


def test_cached_artifacts_survive_original_deletion_and_process_restart(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
) -> None:
    environment = tmp_path / "extension-environment"
    original_directory = tmp_path / "original-wheels"
    original_directory.mkdir()
    original_legacy = Path(
        shutil.copy2(legacy_wheel, original_directory / legacy_wheel.name)
    )
    original_canonical = Path(
        shutil.copy2(
            canonical_wheel,
            original_directory / canonical_wheel.name,
        )
    )
    _initialize(
        upgrade_internals,
        environment,
        original_legacy,
        runtime_dependency_wheels,
    )
    receipt = upgrade_module.upgrade_legacy_distribution(
        environment,
        canonical_wheel=original_canonical,
        legacy_wheel=original_legacy,
        transaction_directory=tmp_path / "transaction",
        host_stopped=True,
    )
    original_legacy.unlink()
    original_canonical.unlink()

    result = subprocess.run(
        [
            str(environment / "bin/python"),
            str(PLUGIN_ROOT / "packaging/common/upgrade_distribution.py"),
            "rollback",
            "--receipt",
            str(receipt.receipt_path),
            "--host-stopped",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["status"] == "rolled_back"
    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["legacy"] == "0.0.9"
    assert inspection["canonical"] is None
