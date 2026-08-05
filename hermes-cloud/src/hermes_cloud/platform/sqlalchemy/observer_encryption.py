"""Tenant envelope encryption for Observer projection bodies."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_cloud.configuration import MAX_SECRET_BYTES
from hermes_cloud.domain.canonical_json import canonical_payload_digest


class ObserverEncryptionError(RuntimeError):
    """Observer body encryption is unavailable or authentication failed."""


def _current_uid() -> int | None:
    provider = getattr(os, "geteuid", None)
    return int(provider()) if callable(provider) else None


def _effective_group_ids() -> frozenset[int]:
    groups = {int(os.getegid())}
    groups.update(int(group_id) for group_id in os.getgroups())
    return frozenset(groups)


@dataclass(frozen=True)
class _ObserverKeyringFileReference:
    path: str
    owner_uid_provider: Callable[[], int | None] = _current_uid
    effective_group_ids_provider: Callable[[], frozenset[int]] = _effective_group_ids

    def read(self) -> str:
        if not Path(self.path).is_absolute():
            raise ObserverEncryptionError("observer keyring path must be absolute")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            before_open = os.lstat(self.path)
            expected_uid = self.owner_uid_provider()
            effective_group_ids = self.effective_group_ids_provider()
            self._validate_metadata(
                before_open,
                expected_uid=expected_uid,
                effective_group_ids=effective_group_ids,
            )
            descriptor = os.open(self.path, flags)
            opened = os.fstat(descriptor)
            self._validate_metadata(
                opened,
                expected_uid=expected_uid,
                effective_group_ids=effective_group_ids,
            )
            if not self._same_identity(before_open, opened):
                raise ObserverEncryptionError("observer keyring changed before open")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                content = stream.read(MAX_SECRET_BYTES + 1)
            after_read = os.lstat(self.path)
            self._validate_metadata(
                after_read,
                expected_uid=expected_uid,
                effective_group_ids=effective_group_ids,
            )
            if not self._same_identity(opened, after_read):
                raise ObserverEncryptionError("observer keyring changed during read")
            if len(content) > MAX_SECRET_BYTES:
                raise ObserverEncryptionError("observer keyring exceeds size limit")
            value = content.decode("utf-8").rstrip("\r\n")
        except ObserverEncryptionError:
            raise
        except (OSError, UnicodeError):
            raise ObserverEncryptionError(
                "observer tenant key registry cannot be loaded safely"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not value:
            raise ObserverEncryptionError("observer keyring must not be empty")
        return value

    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino

    @staticmethod
    def _validate_metadata(
        metadata: os.stat_result,
        expected_uid: int | None,
        effective_group_ids: frozenset[int],
    ) -> None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ObserverEncryptionError(
                "observer keyring must be a regular non-symlink file"
            )
        permissions = stat.S_IMODE(metadata.st_mode)
        private = metadata.st_uid == expected_uid and permissions == 0o600
        shared = (
            metadata.st_uid == 0
            and metadata.st_gid in effective_group_ids
            and permissions == 0o440
        )
        if expected_uid is None or not (private or shared):
            raise ObserverEncryptionError(
                "observer keyring ownership or permissions are invalid"
            )
        if metadata.st_size > MAX_SECRET_BYTES:
            raise ObserverEncryptionError("observer keyring exceeds size limit")


@dataclass(frozen=True, slots=True)
class ObserverEncryptionContext:
    tenant_id: UUID
    agent_id: UUID
    profile: str
    session_key: str
    field: str
    schema_version: int


class TenantObserverCipher(Protocol):
    def encrypt_json(
        self,
        value: object,
        *,
        context: ObserverEncryptionContext,
    ) -> dict[str, object]: ...

    def decrypt_json(
        self,
        envelope: object,
        *,
        context: ObserverEncryptionContext,
    ) -> object: ...


class TenantKekResolver(Protocol):
    def current(self, tenant_id: UUID) -> tuple[str, bytes]: ...

    def resolve(self, tenant_id: UUID, key_version: str) -> bytes: ...


class MappingTenantKekResolver:
    """Immutable tenant key registry retaining historical decrypt versions."""

    def __init__(
        self,
        *,
        keys: Mapping[UUID, Mapping[str, bytes]],
        current_versions: Mapping[UUID, str],
    ) -> None:
        copied: dict[UUID, dict[str, bytes]] = {}
        for tenant_id, versions in keys.items():
            copied_versions: dict[str, bytes] = {}
            for version, key in versions.items():
                if not version or len(version) > 128 or len(key) != 32:
                    raise ObserverEncryptionError(
                        "observer tenant key registry is invalid"
                    )
                copied_versions[version] = bytes(key)
            copied[tenant_id] = copied_versions
        for tenant_id, version in current_versions.items():
            if version not in copied.get(tenant_id, {}):
                raise ObserverEncryptionError(
                    "observer current tenant key is unavailable"
                )
        self._keys = copied
        self._current_versions = dict(current_versions)

    def current(self, tenant_id: UUID) -> tuple[str, bytes]:
        version = self._current_versions.get(tenant_id)
        if version is None:
            raise ObserverEncryptionError("observer tenant key is unavailable")
        return version, self.resolve(tenant_id, version)

    def resolve(self, tenant_id: UUID, key_version: str) -> bytes:
        key = self._keys.get(tenant_id, {}).get(key_version)
        if key is None:
            raise ObserverEncryptionError(
                "observer historical tenant key is unavailable"
            )
        return key

    def __repr__(self) -> str:
        return "MappingTenantKekResolver(<redacted>)"


def read_tenant_kek_registry(path: str) -> MappingTenantKekResolver:
    """Read a strict, secret-file-only tenant KEK registry."""

    def object_without_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, member in pairs:
            if key in value:
                raise ObserverEncryptionError(
                    "observer tenant key registry contains duplicate fields"
                )
            value[key] = member
        return value

    def reject_constant(_value: str) -> None:
        raise ObserverEncryptionError(
            "observer tenant key registry contains non-finite JSON"
        )

    try:
        raw = _ObserverKeyringFileReference(path).read()
        document = json.loads(
            raw,
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    except ObserverEncryptionError:
        raise
    except (json.JSONDecodeError, UnicodeError, TypeError):
        raise ObserverEncryptionError(
            "observer tenant key registry cannot be loaded safely"
        ) from None
    if not isinstance(document, dict) or set(document) != {"version", "tenants"}:
        raise ObserverEncryptionError("observer tenant key registry is invalid")
    tenants = document["tenants"]
    if (
        type(document["version"]) is not int
        or document["version"] != 1
        or not isinstance(tenants, dict)
        or not tenants
    ):
        raise ObserverEncryptionError("observer tenant key registry is invalid")
    keys: dict[UUID, dict[str, bytes]] = {}
    current_versions: dict[UUID, str] = {}
    try:
        for raw_tenant_id, raw_registry in tenants.items():
            tenant_id = UUID(raw_tenant_id)
            if (
                str(tenant_id) != raw_tenant_id
                or not isinstance(raw_registry, dict)
                or set(raw_registry) != {"current", "keys"}
            ):
                raise ValueError
            current = raw_registry["current"]
            raw_keys = raw_registry["keys"]
            if not isinstance(current, str) or not isinstance(raw_keys, dict):
                raise TypeError
            tenant_keys: dict[str, bytes] = {}
            for version, encoded_key in raw_keys.items():
                if not isinstance(version, str) or not isinstance(encoded_key, str):
                    raise TypeError
                tenant_keys[version] = base64.b64decode(
                    encoded_key,
                    validate=True,
                )
            keys[tenant_id] = tenant_keys
            current_versions[tenant_id] = current
    except (ValueError, TypeError, UnicodeError):
        raise ObserverEncryptionError(
            "observer tenant key registry is invalid"
        ) from None
    return MappingTenantKekResolver(
        keys=keys,
        current_versions=current_versions,
    )


class AesGcmTenantEnvelopeCipher:
    """Envelope-encrypt each body with a random DEK wrapped by a KEK."""

    def __init__(self, key_resolver: TenantKekResolver) -> None:
        self._key_resolver = key_resolver

    def encrypt_json(
        self,
        value: object,
        *,
        context: ObserverEncryptionContext,
    ) -> dict[str, object]:
        aad = _aad(context)
        try:
            plaintext = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ObserverEncryptionError(
                "observer plaintext is not canonical JSON"
            ) from error
        key_version, kek = self._key_resolver.current(context.tenant_id)
        kek_fingerprint = _key_fingerprint(kek)
        dek = os.urandom(32)
        wrap_nonce = os.urandom(12)
        payload_nonce = os.urandom(12)
        wrapped_dek_with_tag = AESGCM(kek).encrypt(
            wrap_nonce,
            dek,
            aad + b":dek",
        )
        wrapped_dek, wrap_tag = _split_tag(wrapped_dek_with_tag)
        ciphertext_with_tag = AESGCM(dek).encrypt(payload_nonce, plaintext, aad)
        ciphertext, payload_tag = _split_tag(ciphertext_with_tag)
        return {
            "version": 1,
            "algorithm": "A256GCM",
            "key_version": key_version,
            "kek_fingerprint": kek_fingerprint,
            "wrap_nonce": _encode(wrap_nonce),
            "wrapped_dek": _encode(wrapped_dek),
            "wrap_tag": _encode(wrap_tag),
            "payload_nonce": _encode(payload_nonce),
            "ciphertext": _encode(ciphertext),
            "payload_tag": _encode(payload_tag),
        }

    def decrypt_json(
        self,
        envelope: object,
        *,
        context: ObserverEncryptionContext,
    ) -> object:
        if not isinstance(envelope, dict) or set(envelope) != {
            "version",
            "algorithm",
            "key_version",
            "kek_fingerprint",
            "wrap_nonce",
            "wrapped_dek",
            "wrap_tag",
            "payload_nonce",
            "ciphertext",
            "payload_tag",
        }:
            raise ObserverEncryptionError("observer ciphertext envelope is invalid")
        key_version = envelope["key_version"]
        if (
            envelope["version"] != 1
            or envelope["algorithm"] != "A256GCM"
            or not isinstance(key_version, str)
        ):
            raise ObserverEncryptionError("observer ciphertext key binding is invalid")
        kek = self._key_resolver.resolve(context.tenant_id, key_version)
        if envelope["kek_fingerprint"] != _key_fingerprint(kek):
            raise ObserverEncryptionError("observer ciphertext key binding is invalid")
        aad = _aad(context)
        try:
            wrap_nonce = _decode(envelope["wrap_nonce"], expected_length=12)
            payload_nonce = _decode(envelope["payload_nonce"], expected_length=12)
            wrapped_dek = _decode(envelope["wrapped_dek"])
            wrap_tag = _decode(envelope["wrap_tag"], expected_length=16)
            ciphertext = _decode(envelope["ciphertext"])
            payload_tag = _decode(envelope["payload_tag"], expected_length=16)
            dek = AESGCM(kek).decrypt(
                wrap_nonce,
                wrapped_dek + wrap_tag,
                aad + b":dek",
            )
            if len(dek) != 32:
                raise ObserverEncryptionError("observer wrapped DEK is invalid")
            plaintext = AESGCM(dek).decrypt(
                payload_nonce,
                ciphertext + payload_tag,
                aad,
            )
            return json.loads(plaintext)
        except ObserverEncryptionError:
            raise
        except (
            InvalidTag,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            raise ObserverEncryptionError(
                "observer ciphertext authentication failed"
            ) from error


def _aad(context: ObserverEncryptionContext) -> bytes:
    value: dict[str, Any] = asdict(context)
    value["tenant_id"] = str(context.tenant_id)
    value["agent_id"] = str(context.agent_id)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _split_tag(value: bytes) -> tuple[bytes, bytes]:
    if len(value) <= 16:
        raise ObserverEncryptionError("observer ciphertext is invalid")
    return value[:-16], value[-16:]


def _key_fingerprint(kek: bytes) -> str:
    return canonical_payload_digest({"kek": base64.b64encode(kek).decode("ascii")})


def _decode(value: object, *, expected_length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise ObserverEncryptionError("observer ciphertext encoding is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ObserverEncryptionError(
            "observer ciphertext encoding is invalid"
        ) from error
    if expected_length is not None and len(decoded) != expected_length:
        raise ObserverEncryptionError("observer ciphertext nonce is invalid")
    return decoded
