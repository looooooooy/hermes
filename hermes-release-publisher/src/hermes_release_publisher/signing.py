from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class ReleaseSigningError(RuntimeError):
    """A Release/Channel/Block manifest could not be signed safely."""


def sign_control_payload(
    payload: Mapping[str, Any],
    *,
    private_key_path: Path,
    key_id: str,
    signed_at: str,
) -> dict[str, Any]:
    if not _canonical_identifier(key_id, 96):
        raise ReleaseSigningError("release signing key id is invalid")
    _parse_utc(signed_at, "signed_at")
    _validate_json_value(payload)
    private_key = _read_private_key(private_key_path)
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "key_id": key_id,
        "signature_algorithm": "ed25519",
        "signed_at": signed_at,
        "payload": dict(payload),
        "signature": "",
    }
    signature = private_key.sign(canonical_envelope_bytes(envelope))
    envelope["signature"] = base64.b64encode(signature).decode("ascii")
    return envelope


def build_release_trust_store(
    *,
    private_key_path: Path,
    key_id: str,
    not_before: str,
    not_after: str,
    revoked: bool = False,
) -> dict[str, Any]:
    if not _canonical_identifier(key_id, 96):
        raise ReleaseSigningError("release signing key id is invalid")
    before = _parse_utc(not_before, "not_before")
    after = _parse_utc(not_after, "not_after")
    if before >= after:
        raise ReleaseSigningError("release signing key validity window is invalid")
    private_key = _read_private_key(private_key_path)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "schema_version": 1,
        "keys": [
            {
                "key_id": key_id,
                "signature_algorithm": "ed25519",
                "public_key": base64.b64encode(public_key).decode("ascii"),
                "not_before": not_before,
                "not_after": not_after,
                "revoked": bool(revoked),
            }
        ],
    }


def canonical_envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    if not isinstance(envelope, Mapping):
        raise ReleaseSigningError("signed envelope must be a mapping")
    unsigned = dict(envelope)
    unsigned.pop("signature", None)
    _validate_json_value(unsigned)
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReleaseSigningError("signed envelope is not canonical JSON") from error


def write_json_new(path: Path, payload: Mapping[str, Any], *, mode: int = 0o444) -> None:
    destination = Path(path).resolve(strict=False)
    if destination.exists() or destination.is_symlink():
        raise ReleaseSigningError("signed output already exists")
    parent = destination.parent.resolve(strict=True)
    if parent / destination.name != destination or not parent.is_dir():
        raise ReleaseSigningError("signed output path is not canonical")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_private_key(path: Path) -> Ed25519PrivateKey:
    source = Path(path)
    if not source.is_absolute():
        source = source.resolve()
    if source.is_symlink() or not source.is_file() or source.resolve(strict=True) != source:
        raise ReleaseSigningError("release private key must be a canonical regular non-symlink file")
    before = source.stat()
    if before.st_nlink != 1:
        raise ReleaseSigningError("release private key must not be hardlinked")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and before.st_uid != getuid():
        raise ReleaseSigningError("release private key must be owned by current user")
    if os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
        raise ReleaseSigningError("release private key permissions must be private")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReleaseSigningError("release private key changed while opening")
        key_bytes = os.read(descriptor, 16 * 1024)
    finally:
        os.close(descriptor)
    if not key_bytes.startswith(b"-----BEGIN PRIVATE KEY-----"):
        raise ReleaseSigningError("release private key must be PKCS8 PEM")
    try:
        key = serialization.load_pem_private_key(key_bytes, password=None)
    except (TypeError, ValueError) as error:
        raise ReleaseSigningError("release private key is invalid") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseSigningError("release private key must use Ed25519")
    return key


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        raise ReleaseSigningError("floating-point values are forbidden in signed release control")
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReleaseSigningError("signed release control object keys must be strings")
            _validate_json_value(item)
        return
    raise ReleaseSigningError(f"unsupported signed JSON value type: {type(value).__name__}")


def _parse_utc(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseSigningError(f"{label} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseSigningError(f"{label} must be RFC3339 UTC") from error
    if parsed.tzinfo is None:
        raise ReleaseSigningError(f"{label} must include timezone")
    parsed = parsed.astimezone(UTC)
    if not value.endswith("Z"):
        raise ReleaseSigningError(f"{label} must be UTC")
    return parsed


def _canonical_identifier(value: str, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(character.isalnum() or character in "._-" for character in value)
    )
