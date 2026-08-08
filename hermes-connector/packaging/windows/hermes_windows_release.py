"""Windows runtime evidence bound to the common immutable release identity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_EVIDENCE_BYTES = 16_384


class WindowsReleaseValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WindowsValidatedRelease:
    release_dir: Path
    release_id: str
    release_digest: str
    connector_executable: Path
    connector_executable_sha256: str


def render_windows_runtime_evidence(
    *,
    release_dir: Path,
    expected_release_id: str,
) -> bytes:
    release_dir, manifest = _common_manifest(release_dir, expected_release_id)
    executable = _regular_file(
        release_dir / "connector" / "hermes-connector.exe",
        "Connector executable",
    )
    payload = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "release_digest": manifest["release_digest"],
        "connector_executable_sha256": _sha256(executable),
    }
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def validate_windows_release(
    *,
    release_dir: Path,
    expected_release_id: str,
) -> WindowsValidatedRelease:
    release_dir, manifest = _common_manifest(release_dir, expected_release_id)
    executable = _regular_file(
        release_dir / "connector" / "hermes-connector.exe",
        "Connector executable",
    )
    evidence_path = _regular_file(
        release_dir / "receipts" / "windows-runtime.json",
        "Windows runtime evidence",
    )
    raw = evidence_path.read_bytes()
    if not 1 <= len(raw) <= _MAX_EVIDENCE_BYTES:
        raise WindowsReleaseValidationError("Windows runtime evidence is invalid")
    try:
        evidence = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        raise WindowsReleaseValidationError("Windows runtime evidence is invalid") from None
    expected_fields = {
        "schema_version",
        "release_id",
        "release_digest",
        "connector_executable_sha256",
    }
    if not isinstance(evidence, dict) or set(evidence) != expected_fields:
        raise WindowsReleaseValidationError("Windows runtime evidence schema is invalid")
    if evidence.get("schema_version") != 1:
        raise WindowsReleaseValidationError("Windows runtime evidence version is invalid")
    if (
        evidence.get("release_id") != manifest["release_id"]
        or evidence.get("release_digest") != manifest["release_digest"]
    ):
        raise WindowsReleaseValidationError(
            "Windows runtime evidence is not bound to the common release"
        )
    executable_sha256 = evidence.get("connector_executable_sha256")
    if not isinstance(executable_sha256, str) or _DIGEST.fullmatch(
        executable_sha256
    ) is None:
        raise WindowsReleaseValidationError("Connector executable digest is invalid")
    if _sha256(executable) != executable_sha256:
        raise WindowsReleaseValidationError("Connector executable digest does not match")
    return WindowsValidatedRelease(
        release_dir=release_dir,
        release_id=str(manifest["release_id"]),
        release_digest=str(manifest["release_digest"]),
        connector_executable=executable,
        connector_executable_sha256=executable_sha256,
    )


def _common_manifest(
    release_dir: Path,
    expected_release_id: str,
) -> tuple[Path, dict[str, object]]:
    release_dir = Path(release_dir)
    if not release_dir.is_absolute() or ".." in release_dir.parts:
        raise WindowsReleaseValidationError("release directory must be absolute")
    try:
        canonical = release_dir.resolve(strict=True)
    except OSError:
        raise WindowsReleaseValidationError("release directory is unavailable") from None
    if canonical != release_dir or release_dir.is_symlink() or not release_dir.is_dir():
        raise WindowsReleaseValidationError("release directory must be canonical")
    if release_dir.name != expected_release_id:
        raise WindowsReleaseValidationError("release directory identity does not match")
    manifest_path = _regular_file(
        release_dir / "manifest" / "release.json",
        "common release manifest",
    )
    raw = manifest_path.read_bytes()
    if not 1 <= len(raw) <= _MAX_MANIFEST_BYTES:
        raise WindowsReleaseValidationError("common release manifest is invalid")
    try:
        manifest = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        raise WindowsReleaseValidationError("common release manifest is invalid") from None
    if not isinstance(manifest, dict):
        raise WindowsReleaseValidationError("common release manifest is invalid")
    if manifest.get("schema_version") != 1:
        raise WindowsReleaseValidationError("common release manifest version is invalid")
    if manifest.get("release_id") != expected_release_id:
        raise WindowsReleaseValidationError("common release identity does not match")
    release_digest = manifest.get("release_digest")
    if not isinstance(release_digest, str) or _DIGEST.fullmatch(release_digest) is None:
        raise WindowsReleaseValidationError("common release digest is invalid")
    return release_dir, manifest


def _regular_file(path: Path, name: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise WindowsReleaseValidationError(f"{name} is unavailable")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "WindowsReleaseValidationError",
    "WindowsValidatedRelease",
    "render_windows_runtime_evidence",
    "validate_windows_release",
]
