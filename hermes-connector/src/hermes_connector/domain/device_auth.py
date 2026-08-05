"""Canonical device-auth signing payload and signature codecs."""

from __future__ import annotations

import base64
import binascii

_DOMAIN = b"hermes-device-auth-v1\x00"


def decode_device_signing_payload(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not 64 <= len(value) <= 1_024
        or any(
            not (
                character.isascii() and (character.isalnum() or character in {"-", "_"})
            )
            for character in value
        )
    ):
        raise ValueError("device signing payload is invalid")
    try:
        decoded = base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise ValueError("device signing payload is invalid") from None
    if (
        not 48 <= len(decoded) <= 768
        or _base64url(decoded) != value
        or not decoded.startswith(_DOMAIN)
    ):
        raise ValueError("device signing payload is invalid")
    return decoded


def encode_ed25519_signature(value: bytes) -> str:
    if not isinstance(value, bytes) or len(value) != 64:
        raise ValueError("device Ed25519 signature is invalid")
    return _base64url(value)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
