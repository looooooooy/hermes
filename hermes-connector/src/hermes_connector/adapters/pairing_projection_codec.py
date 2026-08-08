"""Platform-neutral strict codecs for non-secret pairing projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.pairing import PairedProjection, PairingOfferProjection

MAX_PAIRING_PROJECTION_BYTES = 16_384
_SCOPES = frozenset({"session.observe", "session.control.request"})
_LIFECYCLE_STATES = frozenset({"active", "auth_blocked", "suspended", "revoked"})


class UnsafePairingProjection(ValueError):
    """A pairing projection document is invalid or unsafe."""


def encode_pairing_offer_projection(projection: PairingOfferProjection) -> bytes:
    if not isinstance(projection, PairingOfferProjection):
        raise UnsafePairingProjection("pairing projection content is invalid")
    return _json_bytes(
        {
            "credential_fingerprint": projection.credential_fingerprint,
            "expires_at": _format_datetime(projection.expires_at),
            "key_handle": projection.key_handle,
            "pairing_offer_id": str(projection.pairing_offer_id),
            "version": 1,
        }
    )


def decode_pairing_offer_projection(raw: bytes) -> PairingOfferProjection:
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


def encode_paired_projection(projection: PairedProjection) -> bytes:
    if not isinstance(projection, PairedProjection):
        raise UnsafePairingProjection("pairing projection content is invalid")
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


def decode_paired_projection(raw: bytes) -> PairedProjection:
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


def _decode_json(raw: bytes, fields: frozenset[str]) -> dict[str, object]:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_PAIRING_PROJECTION_BYTES:
        raise UnsafePairingProjection("pairing projection content is invalid")
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


def _json_bytes(value: dict[str, object]) -> bytes:
    try:
        raw = json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise UnsafePairingProjection("pairing projection content is invalid") from None
    if not 1 <= len(raw) <= MAX_PAIRING_PROJECTION_BYTES:
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


__all__ = [
    "MAX_PAIRING_PROJECTION_BYTES",
    "UnsafePairingProjection",
    "decode_paired_projection",
    "decode_pairing_offer_projection",
    "encode_paired_projection",
    "encode_pairing_offer_projection",
]
