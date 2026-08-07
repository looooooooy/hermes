"""Signature-verified server-side release catalog.

Storage only supplies bytes. Release identity, channel selection, block policy, minimum-safe
rules, and rollback authorization are accepted only after Ed25519 verification against an
injected public trust store.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .domain import DeviceUpdateContextV1, ReleaseArtifactRefV1, ReleaseUpdateCandidateV1
from .ports import ReleaseControlReaderPort
from .service import UpdateCheckUnavailable

_MAX_CONTROL_BYTES = 1024 * 1024
_ALLOWED_CHANNELS = frozenset({"canary", "beta", "stable", "enterprise"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,159}\Z")


class SignedReleaseCatalog:
    def __init__(
        self,
        *,
        reader: ReleaseControlReaderPort,
        trust_store: Mapping[str, object],
        rollout_basis_points: Mapping[str, int] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._trust = _trust_keys(trust_store)
        self._rollout = _rollout_policy(
            rollout_basis_points
            or {"canary": 500, "beta": 2_500, "stable": 10_000, "enterprise": 10_000}
        )
        self._now = now or (lambda: datetime.now(UTC))

    def select_candidate(self, context: DeviceUpdateContextV1) -> ReleaseUpdateCandidateV1 | None:
        channel_name = context.requested_channel
        if channel_name not in _ALLOWED_CHANNELS:
            raise UpdateCheckUnavailable("requested release channel is unsupported")
        now = _utc(self._now())

        channel_envelope, channel = self._read_signed(
            f"channels/v1/{channel_name}/current.json", now
        )
        _exact(
            channel,
            {
                "schema_version",
                "channel",
                "channel_generation",
                "release_id",
                "release_generation",
                "published_at",
                "minimum_safe_release_generation",
                "mandatory_after",
                "rollback_authorization",
            },
            "Channel",
        )
        if channel.get("schema_version") != 1 or channel.get("channel") != channel_name:
            raise UpdateCheckUnavailable("signed Channel identity is invalid")
        channel_generation = _positive(channel, "channel_generation")
        release_id = _text(channel, "release_id", 160)
        release_generation = _positive(channel, "release_generation")
        _time(_text(channel, "published_at", 64), "channel published_at")
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise UpdateCheckUnavailable("signed Channel release identity is invalid")

        release_envelope, release = self._read_signed(
            f"releases/v1/{release_id}/release-envelope.json", now
        )
        block_envelope, block = self._read_signed("blocks/v1/current.json", now)
        _validate_release(release)
        _validate_block(block)
        if release.get("release_id") != release_id or release.get("release_generation") != release_generation:
            raise UpdateCheckUnavailable("signed Channel target does not match Product Release")

        target = _mapping(_mapping(release, "targets"), context.target)
        _exact(
            target,
            {"minimum_os", "installer", "bootstrap_payload", "managed_release_payload"},
            "release target",
        )
        artifacts = (
            _artifact("installer", _mapping(target, "installer")),
            _artifact("bootstrap_payload", _mapping(target, "bootstrap_payload")),
            _artifact("managed_release_payload", _mapping(target, "managed_release_payload")),
        )

        security = _mapping(release, "security")
        _exact(
            security,
            {"security_critical", "minimum_safe_release_generation", "mandatory_after"},
            "release security",
        )
        security_critical = _boolean(security, "security_critical")
        minimum_safe = max(
            _nonnegative(security, "minimum_safe_release_generation"),
            _nonnegative(channel, "minimum_safe_release_generation"),
            _nonnegative(block, "minimum_safe_release_generation"),
        )
        blocked = any(
            _blocked(item, release_id, release_generation)
            for item in _list(block, "blocked_releases")
        )
        mandatory_after = _earliest(
            _optional_time(security.get("mandatory_after")),
            _optional_time(channel.get("mandatory_after")),
        )
        rollback_authorized = _rollback(
            channel.get("rollback_authorization"),
            context=context,
            release_id=release_id,
            release_generation=release_generation,
            now=now,
        )

        return ReleaseUpdateCandidateV1(
            release_id=release_id,
            product_version=_text(release, "product_version", 64),
            release_generation=release_generation,
            channel=channel_name,
            channel_generation=channel_generation,
            target=context.target,
            minimum_os=_text(target, "minimum_os", 128),
            rollout_basis_points=self._rollout[channel_name],
            minimum_safe_release_generation=minimum_safe,
            security_critical=security_critical,
            mandatory_after=mandatory_after,
            rollback_authorized=rollback_authorized,
            blocked=blocked,
            artifacts=artifacts,
            release_envelope=release_envelope,
            channel_envelope=channel_envelope,
            block_envelope=block_envelope,
        )

    def _read_signed(
        self, object_key: str, now: datetime
    ) -> tuple[dict[str, object], dict[str, object]]:
        try:
            raw = self._reader.read_control_object(object_key)
        except Exception as exc:
            raise UpdateCheckUnavailable("release-control storage read failed") from exc
        document = _json_object(raw, object_key)
        return document, _verify(document, self._trust, now)


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError("release-control reader must return bytes")
    if not raw or len(raw) > _MAX_CONTROL_BYTES:
        raise UpdateCheckUnavailable(f"release-control object is empty or oversized: {label}")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        decoded = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise UpdateCheckUnavailable(f"release-control JSON is invalid: {label}") from exc
    if not isinstance(decoded, dict):
        raise UpdateCheckUnavailable(f"release-control object is not a mapping: {label}")
    return decoded


def _verify(
    envelope: Mapping[str, object],
    trust: Mapping[str, tuple[Ed25519PublicKey, datetime, datetime, bool]],
    now: datetime,
) -> dict[str, object]:
    _exact(
        envelope,
        {"schema_version", "key_id", "signature_algorithm", "signed_at", "payload", "signature"},
        "signed release envelope",
    )
    if envelope.get("schema_version") != 1 or envelope.get("signature_algorithm") != "ed25519":
        raise UpdateCheckUnavailable("release-control envelope schema/signature algorithm is invalid")
    key_id = _text(envelope, "key_id", 96)
    record = trust.get(key_id)
    if record is None:
        raise UpdateCheckUnavailable("release-control signing key is unknown")
    public_key, not_before, not_after, revoked = record
    if revoked:
        raise UpdateCheckUnavailable("release-control signing key is revoked")
    signed_at = _time(_text(envelope, "signed_at", 64), "signed_at")
    if signed_at > now + timedelta(minutes=5) or not not_before <= signed_at < not_after:
        raise UpdateCheckUnavailable("release-control signing time is outside trusted validity")
    try:
        signature = base64.b64decode(_text(envelope, "signature", 256), validate=True)
    except ValueError as exc:
        raise UpdateCheckUnavailable("release-control signature is invalid base64") from exc
    if len(signature) != 64:
        raise UpdateCheckUnavailable("release-control signature length is invalid")
    unsigned = dict(envelope)
    unsigned.pop("signature", None)
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        public_key.verify(signature, canonical)
    except InvalidSignature as exc:
        raise UpdateCheckUnavailable("release-control signature verification failed") from exc
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise UpdateCheckUnavailable("release-control payload is not a mapping")
    return dict(payload)


def _trust_keys(
    store: Mapping[str, object],
) -> dict[str, tuple[Ed25519PublicKey, datetime, datetime, bool]]:
    _exact(store, {"schema_version", "keys"}, "release trust store")
    if store.get("schema_version") != 1:
        raise ValueError("release trust store schema is invalid")
    values = store.get("keys")
    if not isinstance(values, list):
        raise TypeError("release trust store keys must be a list")
    if not 1 <= len(values) <= 32:
        raise ValueError("release trust store key count is invalid")
    result: dict[str, tuple[Ed25519PublicKey, datetime, datetime, bool]] = {}
    for item in values:
        if not isinstance(item, dict):
            raise TypeError("release trust key must be a mapping")
        _exact(
            item,
            {"key_id", "signature_algorithm", "public_key", "not_before", "not_after", "revoked"},
            "release trust key",
        )
        key_id = _text(item, "key_id", 96)
        if key_id in result or item.get("signature_algorithm") != "ed25519":
            raise ValueError("release trust key identity is invalid or duplicated")
        try:
            raw_key = base64.b64decode(_text(item, "public_key", 128), validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(raw_key)
        except ValueError as exc:
            raise ValueError("release trust public key is invalid") from exc
        before = _time(_text(item, "not_before", 64), "not_before")
        after = _time(_text(item, "not_after", 64), "not_after")
        if before >= after:
            raise ValueError("release trust key validity window is invalid")
        result[key_id] = (public_key, before, after, _boolean(item, "revoked"))
    return result


def _validate_release(value: Mapping[str, object]) -> None:
    _exact(
        value,
        {
            "schema_version",
            "product",
            "product_version",
            "release_id",
            "release_generation",
            "published_at",
            "source",
            "components",
            "contracts",
            "targets",
            "security",
        },
        "Product Release",
    )
    if value.get("schema_version") != 1 or value.get("product") != "hermes-desktop":
        raise UpdateCheckUnavailable("Product Release schema/product is invalid")
    release_id = _text(value, "release_id", 160)
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise UpdateCheckUnavailable("Product Release identity is invalid")
    _text(value, "product_version", 64)
    _positive(value, "release_generation")
    _time(_text(value, "published_at", 64), "release published_at")
    if not isinstance(value.get("targets"), dict) or not value["targets"]:
        raise UpdateCheckUnavailable("Product Release target matrix is invalid")
    if not isinstance(value.get("components"), dict) or not isinstance(value.get("contracts"), dict):
        raise UpdateCheckUnavailable("Product Release component/contract matrix is invalid")


def _validate_block(value: Mapping[str, object]) -> None:
    _exact(
        value,
        {"schema_version", "block_generation", "published_at", "minimum_safe_release_generation", "blocked_releases"},
        "Block",
    )
    if value.get("schema_version") != 1:
        raise UpdateCheckUnavailable("signed Block schema is invalid")
    _positive(value, "block_generation")
    _time(_text(value, "published_at", 64), "block published_at")
    if len(_list(value, "blocked_releases")) > 4096:
        raise UpdateCheckUnavailable("signed Block list is oversized")


def _artifact(kind: str, value: Mapping[str, object]) -> ReleaseArtifactRefV1:
    _exact(value, {"object_key", "sha256", "size_bytes", "platform_signature"}, kind)
    object_key = _text(value, "object_key", 1024)
    sha256 = _text(value, "sha256", 64)
    size = _positive(value, "size_bytes")
    parts = object_key.split("/")
    valid_key = (
        len(parts) == 6
        and parts[:3] == ["artifacts", "v1", "sha256"]
        and parts[3] == sha256[:2]
        and parts[4] == sha256
        and bool(parts[5])
        and all(part not in {".", ".."} for part in parts)
    )
    if _SHA256.fullmatch(sha256) is None or size > 8 * 1024 * 1024 * 1024 or not valid_key:
        raise UpdateCheckUnavailable(f"signed {kind} artifact identity is invalid")
    return ReleaseArtifactRefV1(kind=kind, object_key=object_key, sha256=sha256, size_bytes=size)


def _blocked(item: object, release_id: str, release_generation: int) -> bool:
    if not isinstance(item, dict):
        raise TypeError("signed Block entry must be a mapping")
    _exact(item, {"release_id", "release_generation", "reason_code", "blocked_at"}, "Block entry")
    item_id = _text(item, "release_id", 160)
    item_generation = _positive(item, "release_generation")
    _text(item, "reason_code", 96)
    _time(_text(item, "blocked_at", 64), "blocked_at")
    return item_id == release_id or item_generation == release_generation


def _rollback(
    value: object,
    *,
    context: DeviceUpdateContextV1,
    release_id: str,
    release_generation: int,
    now: datetime,
) -> bool:
    if value is None:
        return False
    if not isinstance(value, dict):
        raise TypeError("signed rollback authorization must be a mapping")
    _exact(
        value,
        {"from_release_id", "from_release_generation", "to_release_id", "to_release_generation", "reason_code", "expires_at"},
        "rollback authorization",
    )
    _text(value, "reason_code", 96)
    expires_at = _time(_text(value, "expires_at", 64), "rollback expires_at")
    return (
        context.active_release_id is not None
        and value.get("from_release_id") == context.active_release_id
        and value.get("from_release_generation") == context.active_release_generation
        and value.get("to_release_id") == release_id
        and value.get("to_release_generation") == release_generation
        and now < expires_at
    )


def _rollout_policy(values: Mapping[str, int]) -> dict[str, int]:
    if set(values) != _ALLOWED_CHANNELS:
        raise ValueError("rollout policy must define all release channels")
    result: dict[str, int] = {}
    for channel, basis_points in values.items():
        if type(basis_points) is not int or not 0 <= basis_points <= 10_000:
            raise ValueError(f"rollout basis points are invalid for {channel}")
        result[channel] = basis_points
    return result


def _optional_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("signed optional timestamp must be text")
    return _time(value, "optional timestamp")


def _earliest(first: datetime | None, second: datetime | None) -> datetime | None:
    values = [value for value in (first, second) if value is not None]
    return min(values) if values else None


def _time(value: str, label: str) -> datetime:
    if not value.endswith("Z"):
        raise UpdateCheckUnavailable(f"{label} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UpdateCheckUnavailable(f"{label} must be RFC3339 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise UpdateCheckUnavailable(f"{label} must be UTC")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise UpdateCheckUnavailable("catalog clock returned a naive timestamp")
    return value.astimezone(UTC)


def _exact(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise UpdateCheckUnavailable(f"{label} fields are invalid")


def _mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    item = value.get(name)
    if not isinstance(item, dict):
        raise TypeError(f"signed {name} must be a mapping")
    return dict(item)


def _list(value: Mapping[str, object], name: str) -> list[object]:
    item = value.get(name)
    if not isinstance(item, list):
        raise TypeError(f"signed {name} must be a list")
    return item


def _text(value: Mapping[str, object], name: str, maximum: int) -> str:
    item = value.get(name)
    if not isinstance(item, str):
        raise TypeError(f"signed {name} must be text")
    if not item or len(item) > maximum or "\x00" in item:
        raise UpdateCheckUnavailable(f"signed {name} is invalid")
    return item


def _boolean(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if type(item) is not bool:
        raise TypeError(f"signed {name} must be boolean")
    return item


def _positive(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int:
        raise TypeError(f"signed {name} must be an integer")
    if item <= 0:
        raise UpdateCheckUnavailable(f"signed {name} must be positive")
    return item


def _nonnegative(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int:
        raise TypeError(f"signed {name} must be an integer")
    if item < 0:
        raise UpdateCheckUnavailable(f"signed {name} must be non-negative")
    return item
