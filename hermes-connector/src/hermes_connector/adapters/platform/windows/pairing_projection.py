"""Non-secret pairing projections over Windows private state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar
from uuid import UUID

from hermes_connector.adapters.pairing_projection_codec import (
    MAX_PAIRING_PROJECTION_BYTES,
    UnsafePairingProjection,
    decode_paired_projection,
    decode_pairing_offer_projection,
    encode_paired_projection,
    encode_pairing_offer_projection,
)
from hermes_connector.domain.pairing import PairedProjection, PairingOfferProjection

from .private_state import (
    UnsafeWindowsPrivateState,
    atomic_write_private_file,
    delete_private_file,
    private_named_mutex,
    read_private_file,
    validate_private_directory,
)

_T = TypeVar("_T")


class _WindowsProjectionStore(Generic[_T]):
    __slots__ = ("_decode", "_encode", "_mutex_key", "_path")

    def __init__(
        self,
        path: str | Path,
        *,
        encode: Callable[[_T], bytes],
        decode: Callable[[bytes], _T],
    ) -> None:
        self._path = Path(path)
        self._mutex_key = f"pairing-projection:{self._path}"
        self._encode = encode
        self._decode = decode

    async def load(self) -> _T | None:
        return await asyncio.to_thread(self._load_if_present)

    def load_sync(self) -> _T | None:
        return self._load_if_present()

    async def save(self, projection: _T) -> None:
        raw = self._encode(projection)
        await asyncio.to_thread(self._save, raw)

    async def delete(self) -> bool:
        return await asyncio.to_thread(self._delete)

    def check_path(self) -> None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            raw = read_private_file(
                self._path,
                maximum=MAX_PAIRING_PROJECTION_BYTES,
            )
            if raw is not None:
                self._decode(raw)
        except UnsafePairingProjection:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafePairingProjection("pairing projection path is unsafe") from None

    def _load_if_present(self) -> _T | None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            raw = read_private_file(
                self._path,
                maximum=MAX_PAIRING_PROJECTION_BYTES,
            )
            return None if raw is None else self._decode(raw)
        except UnsafePairingProjection:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafePairingProjection("pairing projection is unavailable") from None

    def _save(self, raw: bytes) -> None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            with private_named_mutex(self._mutex_key):
                existing = read_private_file(
                    self._path,
                    maximum=MAX_PAIRING_PROJECTION_BYTES,
                )
                if existing is not None:
                    self._decode(existing)
                atomic_write_private_file(
                    self._path,
                    raw,
                    maximum=MAX_PAIRING_PROJECTION_BYTES,
                )
        except UnsafePairingProjection:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafePairingProjection(
                "pairing projection could not be saved"
            ) from None

    def _delete(self) -> bool:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            with private_named_mutex(self._mutex_key):
                existing = read_private_file(
                    self._path,
                    maximum=MAX_PAIRING_PROJECTION_BYTES,
                )
                if existing is None:
                    return False
                self._decode(existing)
                return delete_private_file(self._path)
        except UnsafePairingProjection:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafePairingProjection(
                "pairing projection could not be deleted"
            ) from None

    def _validate_reference(self) -> None:
        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafePairingProjection("pairing projection reference is unsafe")


class WindowsPairedProjectionStore(_WindowsProjectionStore[PairedProjection]):
    def __init__(self, path: str | Path) -> None:
        super().__init__(
            path,
            encode=encode_paired_projection,
            decode=decode_paired_projection,
        )


class WindowsPairingOfferProjectionStore(
    _WindowsProjectionStore[PairingOfferProjection]
):
    def __init__(self, path: str | Path) -> None:
        super().__init__(
            path,
            encode=encode_pairing_offer_projection,
            decode=decode_pairing_offer_projection,
        )

    async def delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        if not isinstance(pairing_offer_id, UUID):
            raise TypeError("pairing offer projection version is invalid")
        return await asyncio.to_thread(self._delete_if_matches, pairing_offer_id)

    def _delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            with private_named_mutex(self._mutex_key):
                raw = read_private_file(
                    self._path,
                    maximum=MAX_PAIRING_PROJECTION_BYTES,
                )
                if raw is None:
                    return False
                current = self._decode(raw)
                if current.pairing_offer_id != pairing_offer_id:
                    return False
                return delete_private_file(self._path)
        except UnsafePairingProjection:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafePairingProjection(
                "pairing projection could not be deleted"
            ) from None


__all__ = [
    "UnsafePairingProjection",
    "WindowsPairedProjectionStore",
    "WindowsPairingOfferProjectionStore",
]
