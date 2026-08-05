"""Wheel preflight, cache integrity, and receipt persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from email.parser import Parser
from pathlib import Path

from .models import (
    RECEIPT_SCHEMA_VERSION,
    CachedArtifact,
    UpgradeReceipt,
    UpgradeTransactionError,
    WheelMetadata,
)


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_wheel_metadata(
    wheel: Path,
    expected_distribution: str,
) -> WheelMetadata:
    """Validate one readable wheel and return its declared identity."""
    if not wheel.is_file():
        raise UpgradeTransactionError(f"wheel is not readable: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip() is not None:
                raise UpgradeTransactionError(
                    f"wheel contains a corrupt member: {wheel}"
                )
            metadata_members = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_members) != 1:
                raise UpgradeTransactionError(
                    f"wheel must contain one METADATA file: {wheel}"
                )
            metadata_member = metadata_members[0]
            metadata = Parser().parsestr(archive.read(metadata_member).decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise UpgradeTransactionError(f"wheel is not readable: {wheel}") from error

    distribution = metadata.get("Name", "")
    version = metadata.get("Version", "")
    if (
        _normalize_distribution(distribution)
        != _normalize_distribution(expected_distribution)
        or not version
    ):
        raise UpgradeTransactionError(f"wheel METADATA identity mismatch: {wheel}")
    normalized_name = re.sub(r"[-_.]+", "_", distribution).lower()
    expected_directory = f"{normalized_name}-{version}.dist-info"
    if metadata_member.split("/", 1)[0].lower() != expected_directory:
        raise UpgradeTransactionError(f"wheel METADATA version path mismatch: {wheel}")
    return WheelMetadata(
        distribution=_normalize_distribution(distribution),
        version=version,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_cached_artifact(artifact: CachedArtifact) -> None:
    """Recheck local digest and wheel identity; this is not a signature check."""
    metadata = read_wheel_metadata(
        artifact.path,
        artifact.distribution,
    )
    if (
        metadata.version != artifact.version
        or _sha256(artifact.path) != artifact.sha256
    ):
        raise UpgradeTransactionError(
            f"cached wheel integrity mismatch: {artifact.path}"
        )


def cache_artifact(
    source: Path,
    destination_directory: Path,
    metadata: WheelMetadata,
) -> CachedArtifact:
    """Copy one preflighted wheel into the persistent transaction directory."""
    destination = destination_directory / source.name
    try:
        shutil.copy2(source, destination)
    except OSError as error:
        raise UpgradeTransactionError(f"failed to cache wheel: {source}") from error
    artifact = CachedArtifact(
        distribution=metadata.distribution,
        version=metadata.version,
        path=destination.resolve(),
        sha256=_sha256(destination),
    )
    verify_cached_artifact(artifact)
    return artifact


def persist_receipt(receipt: UpgradeReceipt) -> None:
    """Atomically persist the transaction state before destructive changes."""
    temporary_path = receipt.receipt_path.with_suffix(".json.tmp")
    try:
        temporary_path.write_text(
            json.dumps(
                receipt.to_dict(),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, receipt.receipt_path)
    except OSError as error:
        raise UpgradeTransactionError("failed to persist upgrade receipt") from error


def load_upgrade_receipt(receipt_path: Path) -> UpgradeReceipt:
    """Load a persisted receipt and revalidate both cached artifacts."""
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpgradeTransactionError("upgrade receipt is not readable") from error
    if value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise UpgradeTransactionError("upgrade receipt schema is unsupported")
    receipt = UpgradeReceipt(
        environment=Path(value["environment"]),
        transaction_directory=Path(value["transaction_directory"]),
        legacy_artifact=CachedArtifact.from_dict(value["legacy_artifact"]),
        canonical_artifact=CachedArtifact.from_dict(value["canonical_artifact"]),
        installed_legacy_version=value["installed_legacy_version"],
        completed_steps=tuple(value["completed_steps"]),
        status=value["status"],
    )
    if receipt.receipt_path.resolve() != receipt_path.resolve():
        raise UpgradeTransactionError("upgrade receipt transaction directory mismatch")
    verify_cached_artifact(receipt.legacy_artifact)
    verify_cached_artifact(receipt.canonical_artifact)
    return receipt
