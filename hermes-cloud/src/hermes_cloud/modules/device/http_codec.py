"""Strict public codecs for the device-pairing HTTP contract."""

from __future__ import annotations

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode

_BASE64URL_RAW32 = re.compile(r"^[A-Za-z0-9_-]{43}$")
_INTERNAL_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_SHA256 = re.compile(r"^SHA256:([A-Za-z0-9_-]{43})$")


def _encode_base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_raw32(value: str, *, field: str) -> bytes:
    if not isinstance(value, str) or _BASE64URL_RAW32.fullmatch(value) is None:
        raise ValueError(f"{field} must be canonical base64url")
    try:
        decoded = urlsafe_b64decode(f"{value}=")
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be canonical base64url") from None
    if len(decoded) != 32 or _encode_base64url(decoded) != value:
        raise ValueError(f"{field} must decode to exactly 32 bytes")
    return decoded


def decode_ed25519_public_key(value: str) -> bytes:
    """Decode one unpadded canonical raw-32 Ed25519 public key."""

    return _decode_raw32(value, field="Ed25519 public key")


def internal_fingerprint_from_public(value: str) -> str:
    """Translate the public fingerprint presentation to internal lower hex."""

    if not isinstance(value, str):
        raise TypeError("credential fingerprint must be a string")
    matched = _PUBLIC_SHA256.fullmatch(value)
    if matched is None:
        raise ValueError("credential fingerprint is invalid")
    digest = _decode_raw32(matched.group(1), field="credential fingerprint")
    return digest.hex()


def public_fingerprint_from_internal(value: str) -> str:
    """Translate an internal lower-hex digest to the frozen public form."""

    if not isinstance(value, str) or _INTERNAL_SHA256.fullmatch(value) is None:
        raise ValueError("credential fingerprint is invalid")
    return f"SHA256:{_encode_base64url(bytes.fromhex(value))}"


def public_revision_from_internal(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("internal revision must be a non-negative integer")
    return value + 1


def internal_revision_from_public(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("public revision must be a positive integer")
    return value - 1
