"""Data models and constants for the stopped-host transaction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LEGACY_DISTRIBUTION = "hermes-mobile-gateway"
CANONICAL_DISTRIBUTION = "hermes-agent-plugin"
TARGET_ENTRY_POINT_NAMES = {
    LEGACY_DISTRIBUTION,
    CANONICAL_DISTRIBUTION,
}
UPGRADE_STEPS = (
    "preflight-wheel-metadata-and-installed-legacy-version",
    "validate-legacy-dependencies-entry-point-and-runtime-imports",
    "cache-both-wheels-and-record-sha256",
    "persist-prepared-receipt-before-destructive-change",
    "uninstall-legacy-distribution-before-installing-canonical",
    "install-canonical-distribution-offline-without-dependency-resolution",
    "run-pip-check",
    "validate-canonical-entry-point-ownership-and-runtime-imports",
)
RECEIPT_SCHEMA_VERSION = 1


class UpgradeTransactionError(RuntimeError):
    """The requested upgrade or rollback transaction was rejected."""


class HostMustBeStoppedError(UpgradeTransactionError):
    """The caller did not confirm the required stopped-host boundary."""


@dataclass(frozen=True)
class WheelMetadata:
    distribution: str
    version: str


@dataclass(frozen=True)
class CachedArtifact:
    distribution: str
    version: str
    path: Path
    sha256: str

    def to_dict(self) -> dict:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "path": str(self.path),
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: dict) -> CachedArtifact:
        return cls(
            distribution=value["distribution"],
            version=value["version"],
            path=Path(value["path"]),
            sha256=value["sha256"],
        )


@dataclass
class UpgradeReceipt:
    environment: Path
    transaction_directory: Path
    legacy_artifact: CachedArtifact
    canonical_artifact: CachedArtifact
    installed_legacy_version: str
    completed_steps: tuple[str, ...]
    status: str

    @property
    def receipt_path(self) -> Path:
        return self.transaction_directory / "receipt.json"

    def to_dict(self) -> dict:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "environment": str(self.environment),
            "transaction_directory": str(self.transaction_directory),
            "legacy_artifact": self.legacy_artifact.to_dict(),
            "canonical_artifact": self.canonical_artifact.to_dict(),
            "installed_legacy_version": self.installed_legacy_version,
            "completed_steps": list(self.completed_steps),
            "status": self.status,
        }
