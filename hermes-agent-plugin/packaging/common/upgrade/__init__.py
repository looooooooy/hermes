"""Public API for the stopped-host distribution transaction."""

from .artifacts import load_upgrade_receipt
from .environment import inspect_environment
from .models import (
    HostMustBeStoppedError,
    UpgradeReceipt,
    UpgradeTransactionError,
)
from .transaction import rollback_upgrade, upgrade_legacy_distribution

__all__ = [
    "HostMustBeStoppedError",
    "UpgradeReceipt",
    "UpgradeTransactionError",
    "inspect_environment",
    "load_upgrade_receipt",
    "rollback_upgrade",
    "upgrade_legacy_distribution",
]
