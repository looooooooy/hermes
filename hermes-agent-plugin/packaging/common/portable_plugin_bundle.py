"""Portable vendor-signed Hermes Agent Plugin bundle.

Unlike Plugin Store v1, this format signs only portable artifact identity. Customer
absolute release/store paths are deliberately excluded and are derived locally by the
Runtime Manager after cryptographic verification.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization

from plugin_store_bundle import (
    PluginStoreBundleError,
    _fsync_directory,
    _inspect_wheel,
    _json_bytes,
    _make_immutable,
    _read_private_key,
    _read_regular_file,
    _remove_partial,
    _timestamp,
    _utc,
    _write_file,
)

_PLUGIN_ID = "hermes-agent-plugin"
_ENTRYPOINT = {
    "group": "hermes_agent.plugins",
    "name": _PLUGIN_ID,
    "value": "hermes_agent_plugin",
}
_MAX_WHEEL_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class PortablePluginBundlePaths:
    root: Path
    wheel_path: Path
    manifest_path: Path
    trust_store_path: Path


def _portable_canonical_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    """Freeze portable-v2 signature semantics independently of Plugin Store v1."""

    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PluginStoreBundleError("portable manifest is not canonical JSON") from error


def assemble_portable_plugin_bundle_v2(
    *,
    wheel_path: str | os.PathLike[str],
    private_key_path: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    key_id: str,
    now: datetime,
    issued_at: datetime,
    expires_at: datetime,
    key_not_before: datetime,
    key_not_after: datetime,
) -> PortablePluginBundlePaths:
    """Validate, sign, and atomically publish one portable Plugin artifact identity."""

    output = Path(output_root)
    if not output.is_absolute() or output.resolve(strict=False) != output:
        raise PluginStoreBundleError("portable output root must be an absolute canonical path")
    if output.exists() or output.is_symlink():
        raise PluginStoreBundleError("portable output root already exists")
    parent = output.parent.resolve(strict=True)
    if parent / output.name != output or not parent.is_dir():
        raise PluginStoreBundleError("portable output root parent is invalid")

    observed_now = _utc(now, label="now")
    issued = _utc(issued_at, label="issued_at")
    expires = _utc(expires_at, label="expires_at")
    not_before = _utc(key_not_before, label="key_not_before")
    not_after = _utc(key_not_after, label="key_not_after")
    if observed_now >= expires:
        raise PluginStoreBundleError("portable plugin bundle is expired")
    if issued > observed_now or issued >= expires:
        raise PluginStoreBundleError("portable plugin issued_at/expires_at window is invalid")
    if not_before > issued or observed_now >= not_after or expires > not_after:
        raise PluginStoreBundleError("portable plugin key validity window is invalid")
    if (
        not isinstance(key_id, str)
        or not key_id
        or len(key_id) > 96
        or not all(character.isalnum() or character in "._-" for character in key_id)
    ):
        raise PluginStoreBundleError("portable plugin key id is not canonical")

    private_key = _read_private_key(Path(private_key_path))
    wheel_source = Path(wheel_path)
    wheel_contents = _read_regular_file(
        wheel_source,
        label="plugin wheel",
        limit=_MAX_WHEEL_BYTES,
    )
    version = _inspect_wheel(wheel_contents)
    wheel_sha256 = hashlib.sha256(wheel_contents).hexdigest()
    wheel_name = wheel_source.name
    if "/" in wheel_name or "\\" in wheel_name or wheel_name in {"", ".", ".."}:
        raise PluginStoreBundleError("portable plugin wheel filename is invalid")

    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust_store = {
        "schema_version": 1,
        "keys": [
            {
                "key_id": key_id,
                "signature_algorithm": "ed25519",
                "public_key": base64.b64encode(public_key).decode("ascii"),
                "not_before": _timestamp(not_before),
                "not_after": _timestamp(not_after),
            }
        ],
    }
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "plugin_id": _PLUGIN_ID,
        "version": version,
        "artifact_filename": wheel_name,
        "wheel_sha256": wheel_sha256,
        "entrypoint": dict(_ENTRYPOINT),
        "signature_algorithm": "ed25519",
        "key_id": key_id,
        "issued_at": _timestamp(issued),
        "expires_at": _timestamp(expires),
        "signature": "",
    }
    manifest["signature"] = base64.b64encode(
        private_key.sign(_portable_canonical_manifest_bytes(manifest))
    ).decode("ascii")

    partial = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=parent))
    try:
        staged_wheel = partial / "plugin" / wheel_name
        staged_manifest = partial / "portable-plugin-manifest.json"
        staged_trust = partial / "trust-store.json"
        staged_wheel.parent.mkdir(parents=True, mode=0o700)
        _write_file(staged_wheel, wheel_contents, mode=0o444)
        _write_file(staged_manifest, _json_bytes(manifest), mode=0o444)
        _write_file(staged_trust, _json_bytes(trust_store), mode=0o444)
        if hashlib.sha256(staged_wheel.read_bytes()).hexdigest() != wheel_sha256:
            raise PluginStoreBundleError("portable staged wheel SHA256 mismatch")
        _make_immutable(partial)
        _fsync_directory(staged_wheel.parent)
        _fsync_directory(partial)
        os.replace(partial, output)
        _fsync_directory(parent)
    except Exception:
        _remove_partial(partial)
        raise

    return PortablePluginBundlePaths(
        root=output,
        wheel_path=output / "plugin" / wheel_name,
        manifest_path=output / "portable-plugin-manifest.json",
        trust_store_path=output / "trust-store.json",
    )


def _parse_utc(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PluginStoreBundleError(f"{label} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PluginStoreBundleError(f"{label} must be RFC3339 UTC") from error
    if parsed.tzinfo is None:
        raise PluginStoreBundleError(f"{label} must be RFC3339 UTC")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--issued-at", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--key-not-before", required=True)
    parser.add_argument("--key-not-after", required=True)
    args = parser.parse_args()

    paths = assemble_portable_plugin_bundle_v2(
        wheel_path=args.wheel.resolve(),
        private_key_path=args.private_key.resolve(),
        output_root=args.output.resolve(),
        key_id=args.key_id,
        now=_parse_utc(args.now, "now"),
        issued_at=_parse_utc(args.issued_at, "issued_at"),
        expires_at=_parse_utc(args.expires_at, "expires_at"),
        key_not_before=_parse_utc(args.key_not_before, "key_not_before"),
        key_not_after=_parse_utc(args.key_not_after, "key_not_after"),
    )
    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "schema_version": 2,
                "root": str(paths.root),
                "manifest_path": str(paths.manifest_path),
                "trust_store_path": str(paths.trust_store_path),
                "wheel_path": str(paths.wheel_path),
                "wheel_sha256": manifest["wheel_sha256"],
                "key_id": manifest["key_id"],
                "portable": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PluginStoreBundleError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"portable_plugin_bundle_error: {error}") from error
