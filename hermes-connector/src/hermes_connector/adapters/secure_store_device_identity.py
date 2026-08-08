"""Platform-neutral Ed25519 device identity over a secure secret-store port."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_connector.ports.device_identity import DevicePublicIdentity
from hermes_connector.ports.secure_storage import SecureSecretStorePort

_ALGORITHM = "Ed25519"
_PRIVATE_SEED_BYTES = 32
_MIN_CHALLENGE_BYTES = 16
_MAX_CHALLENGE_BYTES = 4_096


class UnsafeDeviceIdentity(ValueError):
    """The private device identity or requested signing operation is unsafe."""


class SecureStoreDeviceIdentity:
    """Expose stable Ed25519 identity while the private seed stays in a store."""

    __slots__ = ("_random_bytes", "_store")

    def __init__(
        self,
        store: SecureSecretStorePort,
        *,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._store = store
        self._random_bytes = random_bytes

    def check_available(self) -> None:
        try:
            self._store.check_available()
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise UnsafeDeviceIdentity("device secure storage is unavailable") from None

    async def get_or_create(self) -> DevicePublicIdentity:
        secret = await self._read_secret()
        if secret is None:
            seed = self._random_bytes(_PRIVATE_SEED_BYTES)
            if not isinstance(seed, bytes) or len(seed) != _PRIVATE_SEED_BYTES:
                raise UnsafeDeviceIdentity("device key generation failed")
            encoded = _encode_seed(seed)
            try:
                created = await self._store.create_secret(encoded)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - redact all store failures at boundary
                raise UnsafeDeviceIdentity(
                    "device secure storage is unavailable"
                ) from None
            if created:
                secret = encoded
            else:
                secret = await self._read_secret()
                if secret is None:
                    raise UnsafeDeviceIdentity(
                        "device identity creation did not converge"
                    )
        private_key = _private_key_from_secret(secret)
        return _public_identity(private_key)

    async def sign_challenge(self, key_handle: str, challenge: bytes) -> bytes:
        if (
            not isinstance(key_handle, str)
            or not isinstance(challenge, bytes)
            or not _MIN_CHALLENGE_BYTES <= len(challenge) <= _MAX_CHALLENGE_BYTES
        ):
            raise UnsafeDeviceIdentity("device signing request is invalid")
        secret = await self._read_secret()
        if secret is None:
            raise UnsafeDeviceIdentity("device identity is unavailable")
        private_key = _private_key_from_secret(secret)
        public = _public_identity(private_key)
        if key_handle != public.key_handle:
            raise UnsafeDeviceIdentity("device key handle does not match")
        return private_key.sign(challenge)

    async def delete_identity(self, key_handle: str) -> bool:
        if not isinstance(key_handle, str) or not key_handle:
            raise UnsafeDeviceIdentity("device key handle is invalid")
        secret = await self._read_secret()
        if secret is None:
            return False
        private_key = _private_key_from_secret(secret)
        if key_handle != _public_identity(private_key).key_handle:
            raise UnsafeDeviceIdentity("device key handle does not match")
        try:
            return await self._store.delete_secret_if_matches(
                hashlib.sha256(secret).digest()
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise UnsafeDeviceIdentity("device secure storage is unavailable") from None

    async def _read_secret(self) -> bytes | None:
        try:
            return await self._store.read_secret()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise UnsafeDeviceIdentity("device secure storage is unavailable") from None

    def __repr__(self) -> str:
        return "SecureStoreDeviceIdentity(<private-key-redacted>)"


def _private_key_from_secret(secret: bytes) -> Ed25519PrivateKey:
    try:
        seed = _decode_seed(secret)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except (TypeError, ValueError):
        raise UnsafeDeviceIdentity("device identity content is invalid") from None


def _public_identity(private_key: Ed25519PrivateKey) -> DevicePublicIdentity:
    raw_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw_public).digest()
    digest_text = _base64url(digest)
    return DevicePublicIdentity(
        key_handle=f"hermes-device-key:v1:{digest_text}",
        algorithm=_ALGORITHM,
        public_key=_base64url(raw_public),
        fingerprint=f"SHA256:{digest_text}",
    )


def _encode_seed(seed: bytes) -> bytes:
    return _base64url_bytes(seed)


def _decode_seed(value: bytes) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError
    try:
        decoded = base64.b64decode(
            value + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError):
        raise ValueError from None
    if len(decoded) != _PRIVATE_SEED_BYTES or _encode_seed(decoded) != value:
        raise ValueError
    return decoded


def _base64url(value: bytes) -> str:
    return _base64url_bytes(value).decode("ascii")


def _base64url_bytes(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


__all__ = ["SecureStoreDeviceIdentity", "UnsafeDeviceIdentity"]
