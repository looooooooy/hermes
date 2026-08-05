"""Compatibility entry point for the stopped-host upgrade transaction."""

from upgrade import (
    HostMustBeStoppedError,
    UpgradeReceipt,
    UpgradeTransactionError,
    inspect_environment,
    load_upgrade_receipt,
    rollback_upgrade,
    upgrade_legacy_distribution,
)
from upgrade.cli import main

__all__ = [
    "HostMustBeStoppedError",
    "UpgradeReceipt",
    "UpgradeTransactionError",
    "inspect_environment",
    "load_upgrade_receipt",
    "main",
    "rollback_upgrade",
    "upgrade_legacy_distribution",
]


if __name__ == "__main__":
    raise SystemExit(main())
