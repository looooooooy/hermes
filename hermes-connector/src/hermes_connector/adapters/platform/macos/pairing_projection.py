"""Private non-secret pairing projections for macOS."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.pairing import (
    PairedProjection,
    PairingOfferProjection,
)

_MAX_FILE_BYTES = 16_384
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_FLAGS = (
    os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_SCOPES = frozenset({"session.observe", "session.control.request"})
_LIFECYCLE_STATES = frozenset({"active", "auth_blocked", "suspended", "revoked"})
_T = TypeVar("_T")


class UnsafePairingProjection(ValueError):
    """A pairing projection path or document is unsafe."""


class _PrivateProjectionStore(Generic[_T]):
    __slots__ = ("_decode", "_encode", "_lock_path", "_path")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        encode: Callable[[_T], bytes],
        decode: Callable[[bytes], _T],
    ) -> None:
        self._path = Path(path)
        self._lock_path = self._path.parent / f".{self._path.name}.lock"
        self._encode = encode
        self._decode = decode

    async def load(self) -> _T | None:
        return await asyncio.to_thread(self._load_if_present)

    def load_sync(self) -> _T | None:
        """Load during synchronous bootstrap before an event loop exists."""

        return self._load_if_present()

    async def save(self, projection: _T) -> None:
        raw = self._encode(projection)
        await asyncio.to_thread(self._save, raw)

    async def delete(self) -> bool:
        return await asyncio.to_thread(self._delete)

    def check_path(self) -> None:
        self._validate_reference()
        self._validate_parent()
        _validate_lock_path_if_present(self._lock_path)
        if self._path.exists() or self._path.is_symlink():
            self._load()

    def _load_if_present(self) -> _T | None:
        self._validate_reference()
        self._validate_parent()
        if not self._path.exists() and not self._path.is_symlink():
            return None
        return self._load()

    def _load(self) -> _T:
        try:
            before = self._path.lstat()
        except OSError:
            raise UnsafePairingProjection("pairing projection is unavailable") from None
        _validate_file_metadata(before)
        try:
            descriptor = os.open(self._path, _READ_FLAGS)
        except OSError:
            raise UnsafePairingProjection("pairing projection is unavailable") from None
        try:
            opened = os.fstat(descriptor)
            _validate_file_metadata(opened)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise UnsafePairingProjection("pairing projection changed during read")
            raw = _read_all(descriptor, opened.st_size)
        finally:
            os.close(descriptor)
        return self._decode(raw)

    def _save(self, raw: bytes) -> None:
        with _exclusive_projection_lock(self._lock_path):
            self._save_unlocked(raw)

    def _save_unlocked(self, raw: bytes) -> None:
        self._validate_reference()
        self._validate_parent()
        if self._path.exists() or self._path.is_symlink():
            self._load()
        temporary = self._path.parent / (
            f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, _WRITE_FLAGS, 0o600)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        except UnsafePairingProjection:
            raise
        except OSError:
            raise UnsafePairingProjection(
                "pairing projection could not be saved"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise UnsafePairingProjection(
                    "pairing projection temporary cleanup failed"
                ) from None

    def _delete(self) -> bool:
        with _exclusive_projection_lock(self._lock_path):
            return self._delete_unlocked()

    def _delete_unlocked(self) -> bool:
        self._validate_reference()
        self._validate_parent()
        if not self._path.exists() and not self._path.is_symlink():
            return False
        self._load()
        try:
            self._path.unlink()
            _fsync_directory(self._path.parent)
        except OSError:
            raise UnsafePairingProjection(
                "pairing projection could not be deleted"
            ) from None
        return True

    def _validate_reference(self) -> None:
        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafePairingProjection("pairing projection reference is unsafe")

    def _validate_parent(self) -> None:
        try:
            metadata = self._path.parent.lstat()
        except OSError:
            raise UnsafePairingProjection(
                "pairing projection directory is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise UnsafePairingProjection("pairing projection directory is unsafe")


class MacOSPairedProjectionStore(_PrivateProjectionStore[PairedProjection]):
    """Persist the server-authoritative paired binding without its token."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(
            path,
            encode=_encode_paired_projection,
            decode=_decode_paired_projection,
        )


class MacOSPairingOfferProjectionStore(_PrivateProjectionStore[PairingOfferProjection]):
    """Persist only non-secret metadata for one temporary pairing offer."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(
            path,
            encode=_encode_offer_projection,
            decode=_decode_offer_projection,
        )

    async def delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        if not isinstance(pairing_offer_id, UUID):
            raise TypeError("pairing offer projection version is invalid")
        return await asyncio.to_thread(
            self._delete_if_matches,
            pairing_offer_id,
        )

    def _delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        with _exclusive_projection_lock(self._lock_path):
            current = self._load_if_present()
            if current is None or current.pairing_offer_id != pairing_offer_id:
                return False
            return self._delete_unlocked()


def _validate_file_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 1 <= metadata.st_size <= _MAX_FILE_BYTES
    ):
        raise UnsafePairingProjection("pairing projection metadata is unsafe")


def _validate_lock_path_if_present(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise UnsafePairingProjection(
            "pairing projection lock is unavailable"
        ) from None
    _validate_lock_metadata(metadata)


def _validate_lock_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise UnsafePairingProjection("pairing projection lock is unsafe")


@contextmanager
def _exclusive_projection_lock(path: Path):
    try:
        descriptor = os.open(path, _LOCK_FLAGS, 0o600)
    except OSError:
        raise UnsafePairingProjection(
            "pairing projection lock is unavailable"
        ) from None
    locked = False
    try:
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        _validate_lock_metadata(opened)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        current = path.lstat()
        _validate_lock_metadata(current)
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise UnsafePairingProjection("pairing projection lock changed")
        yield
    except UnsafePairingProjection:
        raise
    except OSError:
        raise UnsafePairingProjection("pairing projection lock failed") from None
    finally:
        if locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _read_all(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4_096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size or len(raw) > _MAX_FILE_BYTES:
        raise UnsafePairingProjection("pairing projection changed during read")
    return raw


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise UnsafePairingProjection("pairing projection write failed")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _decode_json(raw: bytes, fields: frozenset[str]) -> dict[str, object]:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
        )
    except (UnicodeDecodeError, ValueError):
        raise UnsafePairingProjection("pairing projection content is invalid") from None
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise UnsafePairingProjection("pairing projection content is invalid")
    return value


def _encode_offer_projection(projection: PairingOfferProjection) -> bytes:
    return _json_bytes(
        {
            "credential_fingerprint": projection.credential_fingerprint,
            "expires_at": _format_datetime(projection.expires_at),
            "key_handle": projection.key_handle,
            "pairing_offer_id": str(projection.pairing_offer_id),
            "version": 1,
        }
    )


def _decode_offer_projection(raw: bytes) -> PairingOfferProjection:
    value = _decode_json(
        raw,
        frozenset(
            {
                "credential_fingerprint",
                "expires_at",
                "key_handle",
                "pairing_offer_id",
                "version",
            }
        ),
    )
    _require_version(value)
    return PairingOfferProjection(
        pairing_offer_id=_uuid(value["pairing_offer_id"]),
        key_handle=_key_handle(value["key_handle"]),
        credential_fingerprint=_fingerprint(value["credential_fingerprint"]),
        expires_at=_datetime(value["expires_at"]),
    )


def _encode_paired_projection(projection: PairedProjection) -> bytes:
    return _json_bytes(
        {
            "agent_id": str(projection.agent_id),
            "credential_fingerprint": projection.credential_fingerprint,
            "credential_id": str(projection.credential_id),
            "device_id": str(projection.device_id),
            "key_handle": projection.key_handle,
            "lifecycle_state": projection.lifecycle_state,
            "scopes": list(projection.scopes),
            "tenant_id": str(projection.tenant_id),
            "token_expires_at": _format_datetime(projection.token_expires_at),
            "version": 1,
        }
    )


def _decode_paired_projection(raw: bytes) -> PairedProjection:
    value = _decode_json(
        raw,
        frozenset(
            {
                "agent_id",
                "credential_fingerprint",
                "credential_id",
                "device_id",
                "key_handle",
                "lifecycle_state",
                "scopes",
                "tenant_id",
                "token_expires_at",
                "version",
            }
        ),
    )
    _require_version(value)
    scopes = value["scopes"]
    if (
        not isinstance(scopes, list)
        or not 1 <= len(scopes) <= 2
        or any(not isinstance(scope, str) or scope not in _SCOPES for scope in scopes)
        or len(set(scopes)) != len(scopes)
    ):
        raise UnsafePairingProjection("pairing projection content is invalid")
    lifecycle = value["lifecycle_state"]
    if not isinstance(lifecycle, str) or lifecycle not in _LIFECYCLE_STATES:
        raise UnsafePairingProjection("pairing projection content is invalid")
    return PairedProjection(
        tenant_id=_uuid(value["tenant_id"]),
        device_id=_uuid(value["device_id"]),
        credential_id=_uuid(value["credential_id"]),
        agent_id=_uuid(value["agent_id"]),
        scopes=tuple(scopes),
        key_handle=_key_handle(value["key_handle"]),
        credential_fingerprint=_fingerprint(value["credential_fingerprint"]),
        token_expires_at=_datetime(value["token_expires_at"]),
        lifecycle_state=lifecycle,
    )


def _json_bytes(value: dict[str, object]) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not 1 <= len(raw) <= _MAX_FILE_BYTES:
        raise UnsafePairingProjection("pairing projection content is invalid")
    return raw


def _require_version(value: dict[str, object]) -> None:
    if type(value["version"]) is not int or value["version"] != 1:
        raise UnsafePairingProjection("pairing projection content is invalid")


def _uuid(value: object) -> UUID:
    try:
        return canonical_uuid(value)
    except (TypeError, ValueError):
        raise UnsafePairingProjection("pairing projection content is invalid") from None


def _key_handle(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("hermes-device-key:v1:")
        or not 24 <= len(value) <= 256
    ):
        raise UnsafePairingProjection("pairing projection content is invalid")
    return value


def _fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("SHA256:")
        or len(value) != 50
    ):
        raise UnsafePairingProjection("pairing projection content is invalid")
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise UnsafePairingProjection("pairing projection content is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise UnsafePairingProjection("pairing projection content is invalid") from None
    if parsed.tzinfo != UTC or _format_datetime(parsed) != value:
        raise UnsafePairingProjection("pairing projection content is invalid")
    return parsed


def _format_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise UnsafePairingProjection("pairing projection content is invalid")
    utc = value.astimezone(UTC)
    if utc.microsecond:
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")
