"""Crash recovery tests for persisted upgrade transaction states."""

from __future__ import annotations

from pathlib import Path

import pytest


class SimulatedProcessCrash(BaseException):
    """Escape transaction error handling like an interrupted process."""


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


def _assert_legacy_restored(upgrade_module, environment: Path) -> None:
    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["legacy"] == "0.0.9"
    assert inspection["canonical"] is None


def _interrupt_before_legacy_uninstall(
    *,
    persisted_status: str,
    environment: Path,
    transaction_directory: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    upgrade_module,
    upgrade_internals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = upgrade_internals.transaction
    if persisted_status == "prepared":
        persist_receipt = transaction.persist_receipt

        def interrupt_persist(receipt) -> None:
            if receipt.status == "in_progress":
                raise SimulatedProcessCrash
            persist_receipt(receipt)

        monkeypatch.setattr(
            transaction,
            "persist_receipt",
            interrupt_persist,
        )
    else:

        def interrupt_uninstall(environment_path, distribution) -> None:
            raise SimulatedProcessCrash

        monkeypatch.setattr(
            transaction,
            "uninstall_distribution",
            interrupt_uninstall,
        )

    with pytest.raises(SimulatedProcessCrash):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=transaction_directory,
            host_stopped=True,
        )
    monkeypatch.undo()


def _delete_legacy_entry_point(environment: Path) -> None:
    site_packages = next((environment / "lib").glob("python*/site-packages"))
    distribution_info = site_packages / "hermes_mobile_gateway-0.0.9.dist-info"
    assert (distribution_info / "METADATA").is_file()
    (distribution_info / "entry_points.txt").unlink()


def test_prepared_receipt_rolls_back_and_repeated_rollback_is_idempotent(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / "extension-environment"
    transaction_directory = tmp_path / "transaction"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    transaction = upgrade_internals.transaction
    persist_receipt = transaction.persist_receipt

    def crash_before_in_progress_persist(receipt) -> None:
        if receipt.status == "in_progress":
            raise SimulatedProcessCrash
        persist_receipt(receipt)

    monkeypatch.setattr(
        transaction,
        "persist_receipt",
        crash_before_in_progress_persist,
    )
    with pytest.raises(SimulatedProcessCrash):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=transaction_directory,
            host_stopped=True,
        )
    monkeypatch.undo()

    receipt = upgrade_module.load_upgrade_receipt(
        transaction_directory / "receipt.json"
    )
    assert receipt.status == "prepared"
    upgrade_module.rollback_upgrade(receipt, host_stopped=True)
    _assert_legacy_restored(upgrade_module, environment)

    reloaded = upgrade_module.load_upgrade_receipt(receipt.receipt_path)
    assert reloaded.status == "rolled_back"
    upgrade_module.rollback_upgrade(reloaded, host_stopped=True)
    _assert_legacy_restored(upgrade_module, environment)


@pytest.mark.parametrize("persisted_status", ("prepared", "in_progress"))
def test_partial_legacy_install_is_repaired_from_cached_wheel(
    persisted_status: str,
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / "extension-environment"
    transaction_directory = tmp_path / "transaction"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    _interrupt_before_legacy_uninstall(
        persisted_status=persisted_status,
        environment=environment,
        transaction_directory=transaction_directory,
        legacy_wheel=legacy_wheel,
        canonical_wheel=canonical_wheel,
        upgrade_module=upgrade_module,
        upgrade_internals=upgrade_internals,
        monkeypatch=monkeypatch,
    )
    _delete_legacy_entry_point(environment)

    receipt = upgrade_module.load_upgrade_receipt(
        transaction_directory / "receipt.json"
    )
    assert receipt.status == persisted_status
    assert upgrade_module.inspect_environment(environment)["legacy"] == "0.0.9"

    upgrade_module.rollback_upgrade(receipt, host_stopped=True)

    assert receipt.status == "rolled_back"
    _assert_legacy_restored(upgrade_module, environment)


def test_partial_legacy_repair_failure_persists_recovery_failed(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / "extension-environment"
    transaction_directory = tmp_path / "transaction"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    _interrupt_before_legacy_uninstall(
        persisted_status="prepared",
        environment=environment,
        transaction_directory=transaction_directory,
        legacy_wheel=legacy_wheel,
        canonical_wheel=canonical_wheel,
        upgrade_module=upgrade_module,
        upgrade_internals=upgrade_internals,
        monkeypatch=monkeypatch,
    )
    _delete_legacy_entry_point(environment)

    def fail_reinstall(*args, **kwargs) -> None:
        raise upgrade_module.UpgradeTransactionError(
            "controlled cached wheel reinstall failure"
        )

    monkeypatch.setattr(
        upgrade_internals.transaction,
        "install_wheel",
        fail_reinstall,
    )
    receipt = upgrade_module.load_upgrade_receipt(
        transaction_directory / "receipt.json"
    )
    with pytest.raises(upgrade_module.UpgradeTransactionError):
        upgrade_module.rollback_upgrade(receipt, host_stopped=True)

    reloaded = upgrade_module.load_upgrade_receipt(receipt.receipt_path)
    assert reloaded.status == "recovery_failed"


def test_interruption_after_legacy_uninstall_is_recoverable(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / "extension-environment"
    transaction_directory = tmp_path / "transaction"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    transaction = upgrade_internals.transaction
    uninstall_distribution = transaction.uninstall_distribution

    def uninstall_then_crash(environment_path, distribution) -> None:
        uninstall_distribution(environment_path, distribution)
        if distribution == "hermes-mobile-gateway":
            raise SimulatedProcessCrash

    monkeypatch.setattr(
        transaction,
        "uninstall_distribution",
        uninstall_then_crash,
    )
    with pytest.raises(SimulatedProcessCrash):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=transaction_directory,
            host_stopped=True,
        )
    monkeypatch.undo()

    receipt = upgrade_module.load_upgrade_receipt(
        transaction_directory / "receipt.json"
    )
    assert receipt.status == "in_progress"
    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["legacy"] is None
    assert inspection["canonical"] is None

    upgrade_module.rollback_upgrade(receipt, host_stopped=True)
    _assert_legacy_restored(upgrade_module, environment)


def test_interruption_after_canonical_install_is_recoverable(
    tmp_path: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / "extension-environment"
    transaction_directory = tmp_path / "transaction"
    _initialize(
        upgrade_internals,
        environment,
        legacy_wheel,
        runtime_dependency_wheels,
    )
    transaction = upgrade_internals.transaction
    install_wheel = transaction.install_wheel

    def install_then_crash(
        environment_path,
        wheel_path,
        *,
        force_reinstall=False,
    ) -> None:
        install_wheel(
            environment_path,
            wheel_path,
            force_reinstall=force_reinstall,
        )
        if Path(wheel_path).parent == transaction_directory:
            raise SimulatedProcessCrash

    monkeypatch.setattr(transaction, "install_wheel", install_then_crash)
    with pytest.raises(SimulatedProcessCrash):
        upgrade_module.upgrade_legacy_distribution(
            environment,
            canonical_wheel=canonical_wheel,
            legacy_wheel=legacy_wheel,
            transaction_directory=transaction_directory,
            host_stopped=True,
        )
    monkeypatch.undo()

    receipt = upgrade_module.load_upgrade_receipt(
        transaction_directory / "receipt.json"
    )
    assert receipt.status == "in_progress"
    inspection = upgrade_module.inspect_environment(environment)
    assert inspection["legacy"] is None
    assert inspection["canonical"] == "0.1.0"

    upgrade_module.rollback_upgrade(receipt, host_stopped=True)
    _assert_legacy_restored(upgrade_module, environment)
