"""Validate PEP 610 dependency provenance against a verified Hermes wheelhouse."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from hermes_offline_wheelhouse import load_verified_wheelhouse

_MAX_DIRECT_URL_BYTES = 64 * 1024
_RELEASE_PROJECT_DIST_INFO = ("hermes_agent-", "hermes_connector-")


class WheelhouseProvenanceError(RuntimeError):
    """Installed dependency provenance escaped the declared release wheelhouse."""


def verify_wheelhouse_direct_urls(
    direct_url_paths: list[str],
    *,
    venv_root: Path,
    wheelhouse_root: Path,
    expected_manifest_sha256: str,
) -> None:
    """Require every dependency direct URL to name an exact verified local wheel."""

    try:
        _verify_wheelhouse_direct_urls(
            direct_url_paths,
            venv_root=venv_root,
            wheelhouse_root=wheelhouse_root,
            expected_manifest_sha256=expected_manifest_sha256,
        )
    except WheelhouseProvenanceError as exc:
        # The exception messages below are fixed policy reasons only; they contain no
        # URL, absolute path, credential, request payload, or tool output.
        print(f"wheelhouse_provenance_error: {exc}", file=sys.stderr, flush=True)
        raise


def _verify_wheelhouse_direct_urls(
    direct_url_paths: list[str],
    *,
    venv_root: Path,
    wheelhouse_root: Path,
    expected_manifest_sha256: str,
) -> None:
    verified = load_verified_wheelhouse(Path(wheelhouse_root))
    if verified.manifest_sha256 != expected_manifest_sha256:
        raise WheelhouseProvenanceError("wheelhouse manifest binding changed")
    root = verified.root.resolve(strict=True)
    declared = {
        artifact.filename: artifact.sha256 for artifact in verified.artifacts
    }
    expected_venv = Path(venv_root).resolve(strict=True)

    for raw_path in direct_url_paths:
        metadata_path = Path(raw_path)
        if (
            metadata_path.is_symlink()
            or not metadata_path.is_file()
            or metadata_path.parent.is_symlink()
        ):
            raise WheelhouseProvenanceError(
                "dependency direct_url metadata is missing or symlinked"
            )
        metadata_path = metadata_path.resolve(strict=True)
        try:
            metadata_path.relative_to(expected_venv)
        except ValueError as exc:
            raise WheelhouseProvenanceError(
                "dependency direct_url metadata escaped isolated venv"
            ) from exc
        if metadata_path.stat().st_size > _MAX_DIRECT_URL_BYTES:
            raise WheelhouseProvenanceError("dependency direct_url metadata is oversized")

        # Core and Connector are installed as the two final release project wheels, not
        # from the dependency wheelhouse. Their exact wheel SHA-256 values are already
        # bound and verified independently by ReleaseInputs / the release manifest.
        # The runtime verifier historically attempted to exclude these records itself,
        # but normalized '-' to '_' and then tested a '-' separator, making the filter
        # impossible to match. Exclude only these exact project dist-info families here;
        # every third-party dependency continues through strict wheelhouse provenance.
        if _is_release_project_metadata(metadata_path):
            continue

        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WheelhouseProvenanceError(
                "dependency direct_url metadata is invalid"
            ) from exc
        if not isinstance(value, dict) or set(value) - {
            "url",
            "archive_info",
            "dir_info",
            "vcs_info",
            "subdirectory",
        }:
            raise WheelhouseProvenanceError(
                "dependency direct_url metadata fields are invalid"
            )
        url = value.get("url")
        if not isinstance(url, str) or not url:
            raise WheelhouseProvenanceError("dependency direct_url URL is invalid")
        parsed = urlparse(url)
        if parsed.scheme.casefold() != "file" or parsed.query or parsed.fragment:
            raise WheelhouseProvenanceError("dependency provenance is not a local wheel")
        if parsed.netloc not in {"", "localhost"}:
            raise WheelhouseProvenanceError(
                "dependency file URL uses a remote authority"
            )

        decoded_path = url2pathname(unquote(parsed.path))
        if (
            os.name == "nt"
            and len(decoded_path) >= 3
            and decoded_path[0] in {"/", "\\"}
            and decoded_path[2] == ":"
        ):
            decoded_path = decoded_path[1:]
        wheel = Path(decoded_path)
        if (
            wheel.is_symlink()
            or not wheel.is_file()
            or wheel.suffix.casefold() != ".whl"
        ):
            raise WheelhouseProvenanceError(
                "dependency provenance does not reference a regular wheel"
            )
        wheel = wheel.resolve(strict=True)
        if wheel.parent != root:
            raise WheelhouseProvenanceError(
                "dependency provenance escaped verified wheelhouse"
            )
        expected_sha = declared.get(wheel.name)
        if expected_sha is None:
            raise WheelhouseProvenanceError(
                "dependency wheel is not declared by wheelhouse"
            )
        if _sha256(wheel) != expected_sha:
            raise WheelhouseProvenanceError(
                "dependency wheel digest does not match wheelhouse"
            )

        archive_info = value.get("archive_info")
        if archive_info is not None:
            if not isinstance(archive_info, dict):
                raise WheelhouseProvenanceError("dependency archive_info is invalid")
            hashes = archive_info.get("hashes")
            single_hash = archive_info.get("hash")
            if isinstance(hashes, dict) and "sha256" in hashes:
                if hashes["sha256"] != expected_sha:
                    raise WheelhouseProvenanceError(
                        "dependency PEP 610 SHA-256 disagrees with wheelhouse"
                    )
            elif isinstance(single_hash, str) and single_hash.startswith("sha256="):
                if single_hash.removeprefix("sha256=") != expected_sha:
                    raise WheelhouseProvenanceError(
                        "dependency PEP 610 SHA-256 disagrees with wheelhouse"
                    )


def _is_release_project_metadata(metadata_path: Path) -> bool:
    parent_name = metadata_path.parent.name.casefold()
    return parent_name.endswith(".dist-info") and any(
        parent_name.startswith(prefix) for prefix in _RELEASE_PROJECT_DIST_INFO
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
