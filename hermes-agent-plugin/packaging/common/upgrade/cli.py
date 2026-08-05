"""Standalone CLI used by the external installer or Connector Updater."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .artifacts import load_upgrade_receipt
from .environment import inspect_environment
from .models import UpgradeTransactionError
from .transaction import rollback_upgrade, upgrade_legacy_distribution


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the stopped-host plugin distribution transaction.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument("--environment", type=Path, required=True)
    upgrade.add_argument("--legacy-wheel", type=Path, required=True)
    upgrade.add_argument("--canonical-wheel", type=Path, required=True)
    upgrade.add_argument(
        "--transaction-directory",
        type=Path,
        required=True,
    )
    upgrade.add_argument("--host-stopped", action="store_true")

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--receipt", type=Path, required=True)
    rollback.add_argument("--host-stopped", action="store_true")

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--environment", type=Path, required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    """Run the standalone installer-facing CLI."""
    parsed = _argument_parser().parse_args(arguments)
    try:
        if parsed.command == "upgrade":
            receipt = upgrade_legacy_distribution(
                parsed.environment,
                canonical_wheel=parsed.canonical_wheel,
                legacy_wheel=parsed.legacy_wheel,
                transaction_directory=parsed.transaction_directory,
                host_stopped=parsed.host_stopped,
            )
            output = {
                "receipt": str(receipt.receipt_path),
                "status": receipt.status,
            }
        elif parsed.command == "rollback":
            receipt = load_upgrade_receipt(parsed.receipt)
            rollback_upgrade(
                receipt,
                host_stopped=parsed.host_stopped,
            )
            output = {
                "receipt": str(receipt.receipt_path),
                "status": receipt.status,
            }
        else:
            output = inspect_environment(parsed.environment)
        print(json.dumps(output, sort_keys=True))
    except UpgradeTransactionError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0
