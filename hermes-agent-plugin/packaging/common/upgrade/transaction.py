"""Stopped-host upgrade and rollback orchestration."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .artifacts import (
    cache_artifact,
    persist_receipt,
    read_wheel_metadata,
    verify_cached_artifact,
)
from .environment import (
    create_environment,
    inspect_environment,
    install_bundle_wheels,
    install_wheel,
    pip_check,
    uninstall_distribution,
    validate_canonical,
    validate_legacy,
)
from .models import (
    CANONICAL_DISTRIBUTION,
    LEGACY_DISTRIBUTION,
    UPGRADE_STEPS,
    HostMustBeStoppedError,
    UpgradeReceipt,
    UpgradeTransactionError,
    WheelMetadata,
)


def initialize_legacy_environment(
    environment: Path,
    legacy_wheel: Path,
    *,
    bundle_wheels: Iterable[Path] = (),
) -> Path:
    """Create a testable environment from a trusted, locked Bundle."""
    if environment.exists():
        raise UpgradeTransactionError("extension environment must not already exist")
    metadata = read_wheel_metadata(
        legacy_wheel,
        LEGACY_DISTRIBUTION,
    )
    create_environment(environment)
    install_bundle_wheels(environment, bundle_wheels)
    install_wheel(environment, legacy_wheel)
    validate_legacy(environment, metadata.version)
    return environment


def _prepare_receipt(
    environment: Path,
    transaction_directory: Path,
    legacy_wheel: Path,
    canonical_wheel: Path,
    legacy_metadata: WheelMetadata,
    canonical_metadata: WheelMetadata,
) -> UpgradeReceipt:
    if transaction_directory.exists():
        raise UpgradeTransactionError("transaction directory must not already exist")
    try:
        transaction_directory.mkdir(parents=True)
    except OSError as error:
        raise UpgradeTransactionError(
            "failed to create transaction directory"
        ) from error
    receipt = UpgradeReceipt(
        environment=environment.resolve(),
        transaction_directory=transaction_directory.resolve(),
        legacy_artifact=cache_artifact(
            legacy_wheel,
            transaction_directory,
            legacy_metadata,
        ),
        canonical_artifact=cache_artifact(
            canonical_wheel,
            transaction_directory,
            canonical_metadata,
        ),
        installed_legacy_version=legacy_metadata.version,
        completed_steps=UPGRADE_STEPS[:4],
        status="prepared",
    )
    persist_receipt(receipt)
    return receipt


def _restore_legacy(receipt: UpgradeReceipt) -> None:
    verify_cached_artifact(receipt.legacy_artifact)
    verify_cached_artifact(receipt.canonical_artifact)
    uninstall_distribution(
        receipt.environment,
        CANONICAL_DISTRIBUTION,
    )
    install_wheel(
        receipt.environment,
        receipt.legacy_artifact.path,
        force_reinstall=True,
    )
    validate_legacy(
        receipt.environment,
        receipt.legacy_artifact.version,
    )
    receipt.status = "legacy_restored"
    persist_receipt(receipt)


def upgrade_legacy_distribution(
    environment: Path,
    *,
    canonical_wheel: Path,
    legacy_wheel: Path,
    transaction_directory: Path,
    host_stopped: bool,
) -> UpgradeReceipt:
    """Run a cached, stopped-host replacement without dependency resolution."""
    if not host_stopped:
        raise HostMustBeStoppedError(
            "Hermes Agent must be stopped for distribution migration"
        )

    legacy_metadata = read_wheel_metadata(
        legacy_wheel,
        LEGACY_DISTRIBUTION,
    )
    canonical_metadata = read_wheel_metadata(
        canonical_wheel,
        CANONICAL_DISTRIBUTION,
    )
    validate_legacy(environment, legacy_metadata.version)
    receipt = _prepare_receipt(
        environment,
        transaction_directory,
        legacy_wheel,
        canonical_wheel,
        legacy_metadata,
        canonical_metadata,
    )

    try:
        verify_cached_artifact(receipt.legacy_artifact)
        verify_cached_artifact(receipt.canonical_artifact)
        receipt.status = "in_progress"
        persist_receipt(receipt)
        uninstall_distribution(environment, LEGACY_DISTRIBUTION)
        receipt.completed_steps = UPGRADE_STEPS[:5]
        persist_receipt(receipt)
        install_wheel(environment, receipt.canonical_artifact.path)
        receipt.completed_steps = UPGRADE_STEPS[:6]
        persist_receipt(receipt)
        pip_check(environment)
        receipt.completed_steps = UPGRADE_STEPS[:7]
        persist_receipt(receipt)
        validate_canonical(
            environment,
            receipt.canonical_artifact.version,
        )
        receipt.completed_steps = UPGRADE_STEPS
        receipt.status = "completed"
        persist_receipt(receipt)
    except UpgradeTransactionError as upgrade_error:
        try:
            _restore_legacy(receipt)
        except UpgradeTransactionError as rollback_error:
            receipt.status = "recovery_failed"
            persist_receipt(receipt)
            raise UpgradeTransactionError(
                "upgrade and automatic legacy restoration both failed"
            ) from rollback_error
        raise UpgradeTransactionError(
            "upgrade failed; legacy distribution restored"
        ) from upgrade_error
    return receipt


def rollback_upgrade(
    receipt: UpgradeReceipt,
    *,
    host_stopped: bool,
) -> None:
    """Idempotently restore cached legacy state after upgrade or interruption."""
    if not host_stopped:
        raise HostMustBeStoppedError(
            "Hermes Agent must be stopped for distribution rollback"
        )
    rollbackable_statuses = {
        "prepared",
        "in_progress",
        "completed",
        "legacy_restored",
        "rolled_back",
    }
    if receipt.status not in rollbackable_statuses:
        raise UpgradeTransactionError(
            f"receipt status is not rollbackable: {receipt.status}"
        )

    verify_cached_artifact(receipt.legacy_artifact)
    verify_cached_artifact(receipt.canonical_artifact)
    inspection = inspect_environment(receipt.environment)
    installed_legacy = inspection["legacy"]
    installed_canonical = inspection["canonical"]
    if installed_legacy not in {
        None,
        receipt.legacy_artifact.version,
    }:
        raise UpgradeTransactionError(
            "installed legacy version does not match cached rollback artifact"
        )
    if installed_canonical not in {
        None,
        receipt.canonical_artifact.version,
    }:
        raise UpgradeTransactionError(
            "installed canonical version does not match cached upgrade artifact"
        )
    canonical_was_installed = installed_canonical is not None

    if installed_legacy is not None and not canonical_was_installed:
        try:
            validate_legacy(
                receipt.environment,
                receipt.legacy_artifact.version,
            )
        except UpgradeTransactionError:
            pass
        else:
            receipt.status = "rolled_back"
            persist_receipt(receipt)
            return

    try:
        if canonical_was_installed:
            uninstall_distribution(
                receipt.environment,
                CANONICAL_DISTRIBUTION,
            )
        install_wheel(
            receipt.environment,
            receipt.legacy_artifact.path,
            force_reinstall=True,
        )
        validate_legacy(
            receipt.environment,
            receipt.legacy_artifact.version,
        )
        receipt.status = "rolled_back"
        persist_receipt(receipt)
    except UpgradeTransactionError as rollback_error:
        if not canonical_was_installed:
            receipt.status = "recovery_failed"
            persist_receipt(receipt)
            raise UpgradeTransactionError(
                "legacy recovery failed after interrupted upgrade"
            ) from rollback_error
        uninstall_distribution(
            receipt.environment,
            LEGACY_DISTRIBUTION,
        )
        install_wheel(
            receipt.environment,
            receipt.canonical_artifact.path,
            force_reinstall=True,
        )
        validate_canonical(
            receipt.environment,
            receipt.canonical_artifact.version,
        )
        receipt.status = "rollback_failed_canonical_restored"
        persist_receipt(receipt)
        raise UpgradeTransactionError(
            "legacy rollback failed; canonical distribution restored"
        ) from rollback_error
