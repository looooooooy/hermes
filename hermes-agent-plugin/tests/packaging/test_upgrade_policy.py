"""Upgrade policy and stopped-host boundary tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
UPGRADE_RULES_PATH = PLUGIN_ROOT / "packaging/common/legacy-to-canonical.json"


def test_upgrade_rules_define_external_bundle_and_integrity_boundaries() -> None:
    rules = json.loads(UPGRADE_RULES_PATH.read_text(encoding="utf-8"))

    assert rules["legacy_distribution"] == "hermes-mobile-gateway"
    assert rules["canonical_distribution"] == "hermes-agent-plugin"
    assert rules["strategy"] == "host-stopped-in-place-transaction"
    assert rules["requires_host_stopped"] is True
    assert rules["atomic_host_activation"] is False
    assert rules["bundle_dependency_policy"] == (
        "trusted-locked-bundle-preinstalls-runtime-dependencies"
    )
    assert rules["wheel_install_mode"] == "offline-no-deps"
    assert rules["artifact_digest_role"] == (
        "integrity-check-only-not-signature-verification"
    )
    assert rules["receipt_statuses"] == [
        "prepared",
        "in_progress",
        "completed",
        "legacy_restored",
        "rolled_back",
        "recovery_failed",
        "rollback_failed_canonical_restored",
    ]
    assert rules["invoked_by"] == [
        "external-signed-installer",
        "connector-updater",
    ]
    assert rules["included_in_plugin_wheel"] is False
    assert rules["steps"] == [
        "preflight-wheel-metadata-and-installed-legacy-version",
        "validate-legacy-dependencies-entry-point-and-runtime-imports",
        "cache-both-wheels-and-record-sha256",
        "persist-prepared-receipt-before-destructive-change",
        "uninstall-legacy-distribution-before-installing-canonical",
        "install-canonical-distribution-offline-without-dependency-resolution",
        "run-pip-check",
        "validate-canonical-entry-point-ownership-and-runtime-imports",
    ]


def test_upgrade_requires_explicit_stopped_host_boundary(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
) -> None:
    environment = tmp_path / "extension-environment"
    upgrade_internals.initialize_legacy_environment(
        environment,
        legacy_wheel,
        bundle_wheels=runtime_dependency_wheels,
    )

    with pytest.raises(upgrade_module.HostMustBeStoppedError):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=tmp_path / "transaction",
            host_stopped=False,
        )

    assert upgrade_module.inspect_environment(environment)["legacy"] == "0.0.9"


def test_public_upgrade_api_has_no_unguarded_mutation_bypass(
    upgrade_module,
) -> None:
    import upgrade

    expected = {
        "HostMustBeStoppedError",
        "UpgradeReceipt",
        "UpgradeTransactionError",
        "inspect_environment",
        "load_upgrade_receipt",
        "rollback_upgrade",
        "upgrade_legacy_distribution",
    }
    forbidden = {
        "create_environment",
        "initialize_legacy_environment",
        "install_bundle_wheels",
        "install_wheel",
        "uninstall_distribution",
    }

    assert set(upgrade.__all__) == expected
    assert set(upgrade_module.__all__) == expected | {"main"}
    for name in forbidden:
        assert not hasattr(upgrade, name)
        assert not hasattr(upgrade_module, name)
