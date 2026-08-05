"""Dependency and Entry Point ownership upgrade tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


def _initialize(
    upgrade_internals,
    environment: Path,
    legacy_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    *,
    extra_wheels: tuple[Path, ...] = (),
) -> None:
    upgrade_internals.initialize_legacy_environment(
        environment,
        legacy_wheel,
        bundle_wheels=(*runtime_dependency_wheels, *extra_wheels),
    )


@pytest.mark.parametrize(
    "candidate_requirement",
    ("candidate-runtime>=2", "websockets>=17"),
)
def test_candidate_dependency_failure_restores_legacy(
    tmp_path: Path,
    legacy_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    canonical_variant_factory: Callable[[str], Path],
    candidate_requirement: str,
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
    candidate_wheel = canonical_variant_factory(candidate_requirement)
    transaction_directory = tmp_path / "transaction"

    with pytest.raises(upgrade_module.UpgradeTransactionError):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=candidate_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=transaction_directory,
            host_stopped=True,
        )

    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["legacy"] == "0.0.9"
    assert inspection["canonical"] is None
    loaded = upgrade_module.load_upgrade_receipt(transaction_directory / "receipt.json")
    assert loaded.status == "legacy_restored"


def test_duplicate_target_entry_point_owner_is_rejected(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    plugin_wheel_factory: Callable[[str, str], Path],
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
    duplicate_target = plugin_wheel_factory(
        "duplicate-hermes-plugin",
        "hermes-agent-plugin",
    )
    upgrade_internals.install_bundle_wheels(
        environment,
        (duplicate_target,),
    )

    with pytest.raises(upgrade_module.UpgradeTransactionError):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=tmp_path / "transaction",
            host_stopped=True,
        )

    assert upgrade_module.inspect_environment(environment)["legacy"] == "0.0.9"


def test_unrelated_hermes_plugin_is_allowed_and_preserved(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    plugin_wheel_factory: Callable[[str, str], Path],
    upgrade_module,
    upgrade_internals,
) -> None:
    environment = tmp_path / "extension-environment"
    unrelated_plugin = plugin_wheel_factory(
        "other-hermes-plugin",
        "other-hermes-plugin",
    )
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
        extra_wheels=(unrelated_plugin,),
    )

    upgrade_module.upgrade_legacy_distribution(
        environment,
        canonical_wheel=canonical_wheel,
        legacy_wheel=legacy_wheel,
        transaction_directory=tmp_path / "transaction",
        host_stopped=True,
    )

    inspection = upgrade_module.inspect_environment(environment)
    assert {
        (
            entry_point["name"],
            entry_point["distribution"],
        )
        for entry_point in inspection["entry_points"]
    } == {
        ("hermes-agent-plugin", "hermes-agent-plugin"),
        ("other-hermes-plugin", "other-hermes-plugin"),
    }
