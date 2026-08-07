"""Signature-verified server-side release catalog.

Storage is deliberately abstract. The catalog reads bounded Release/Channel/Block control
objects, verifies Ed25519 signatures against an injected public trust store, validates the
relationships between those objects, and only then maps them into an UpdateCheck candidate.
OSS/CDN/database adapters may provide bytes but cannot define release identity or policy.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .domain import (
    DeviceUpdateContextV1,
    ReleaseArtifactRefV1,
    ReleaseUpdateCandidateV1,
)
from .ports import ReleaseControlReaderPort
from .service import UpdateCheckUnavailable

_MAX_CONTROL_BYTES = 1024 * 1024
_MAX_TRUST_KEYS = 32
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
        self._trust = _parse_trust_store(trust_store)
        self._rollout = _validate_rollout(
            rollout_basis_points
            or {
                "canary": 500,
                "beta": 2_500,
                "stable": 10_000,
                "enterprise": 10_000,
            }
        )
        self._now = now or (lambda: datetime.now(UTC))

    def select_candidate(
        self,
        context: DeviceUpdateContextV1,
    ) -> ReleaseUpdateCandidateV1 | None:
        if context.requested_channel not in _ALLOWED_CHANNELS:
            raise UpdateCheckUnavailable("requested release channel is unsupported")
        observed_now = _utc(self._now())

        channel_envelope, channel = self._read_signed(
            f"channels/v1/{context.requested_channel}/current.json",
            observed_now,
        )
        _validate_channel(channel, context.requested_channel)
        release_id = _required_text(channel, "release_id", 160)
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise UpdateCheckUnavailable("signed channel release identity is invalid")

        release_envelope, release = self._read_signed(
            f"releases/v1/{release_id}/release-envelope.json",
            observed_now,
        )
        block_envelope, block = self._read_signed("blocks/v1/current.json", observed_now)
        _validate_release(release)
        _validate_block(block)

        release_generation = _positive_int(release, "release_generation")
        if channel.get("release_id") != release_id or channel.get("release_generation") != release_generation:
            raise UpdateCheckUnavailable("signed channel target does not match Product Release")

        target = _required_mapping(_required_mapping(release, "targets"), context.target)
        _require_exact_keys(
            target,
            {"minimum_os", "installer", "bootstrap_payload", "managed_release_payload"},
            "release target",
        )
        minimum_os = _required_text(target, "minimum_os", 128)

        artifacts = (
            _artifact("installer", _required_mapping(target, "installer")),
            _artifact("bootstrap_payload", _required_mapping(target, "bootstrap_payload")),
            _artifact(
                "managed_release_payload",
                _required_mapping(target, "managed_release_payload"),
            ),
        )

        security = _required_mapping(release, "security")
        _require_exact_keys(
            security,
            {"security_critical", "minimum_safe_release_generation", "mandatory_after"},
            "release security",
        )
        security_critical = _required_bool(security, "security_critical")
        release_minimum = _nonnegative_int(security, "minimum_safe_release_generation")
        channel_minimum = _nonnegative_int(channel, "minimum_safe_release_generation")
        block_minimum = _nonnegative_int(block, "minimum_safe_release_generation")
        minimum_safe = max(release_minimum, channel_minimum, block_minimum)

        blocked = any(
            _blocked_matches(entry, release_id, release_generation)
            for entry in _required_list(block, "blocked_releases")
        )
        mandatory_after = _earliest_time(
            _optional_utc(security.get("mandatory_after")),
            _optional_utc(channel.get("mandatory_after")),
        )
        rollback_authorized = _rollback_authorized(
            channel.get("rollback_authorization"),
            context=context,
            target_release_id=release_id,
            target_generation=release_generation,
            now=observed_now,
        )

        product_version = _required_text(release, "product_version", 64)
        channel_generation = _positive_int(channel, "channel_generation")
        return ReleaseUpdateCandidateV1(
            release_id=release_id,
            product_version=product_version,
            release_generation=release_generation,
            channel=context.requested_channel,
            channel_generation=channel_generation,
            target=context.target,
            minimum_os=minimum_os,
            rollout_basis_points=self._rollout[context.requested_channel],
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
        self,
        object_key: str,
        now: datetime,
    ) -> tuple[dict[str, object], dict[str, object]]:
        try:
            raw = self._reader.read_control_object(object_key)
        except Exception as exc:
            raise UpdateCheckUnavailable("release-control storage read failed") from exc
        document = _strict_json(raw, object_key)
        payload = _verify_envelope(document, self._trust, now)
        return document, payload


def _strict_json(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_CONTROL_BYTES:
        raise UpdateCheckUnavailable(f"release-control object is empty or oversized: {label}")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise UpdateCheckUnavailable(f"release-control JSON is invalid: {label}") from exc
    if not isinstance(value, dict):
        raise UpdateCheckUnavailable(f"release-control object is not a mapping: {label}")
    return value


def _verify_envelope(
    envelope: Mapping[str, object],
    trust: Mapping[str, tuple[Ed25519PublicKey, datetime, datetime, bool]],
    now: datetime,
) -> dict[str, object]:
    _require_exact_keys(
        envelope,
        {"schema_version", "key_id", "signature_algorithm", "signed_at", "payload", "signature"},
        "signed release envelope",
    )
    if envelope.get("schema_version") != 1 or envelope.get("signature_algorithm") != "ed25519":
        raise UpdateCheckUnavailable("release-control envelope schema/signature algorithm is invalid")
    key_id = _required_text(envelope, "key_id", 96)
    key_record = trust.get(key_id)
    if key_record is None:
        raise UpdateCheckUnavailable("release-control signing key is unknown")
    public_key, not_before, not_after, revoked = key_record
    if revoked:
        raise UpdateCheckUnavailable("release-control signing key is revoked")
    signed_at = _parse_utc(_required_text(envelope, "signed_at", 64), "signed_at")
    if signed_at > now + timedelta(minutes=5) or not (not_before <= signed_at < not_after):
        raise UpdateCheckUnavailable("release-control signing time is outside trusted validity")
    signature_text = _required_text(envelope, "signature", 256)
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError) as exc:
        raise UpdateCheckUnavailable("release-control signature is invalid base64") from exc
    if len(signature) != 64:
        raise UpdateCheckUnavailable("release-control signature length is invalid")
    unsigned = dict(envelope)
    unsigned.pop("signature", None)
    try:
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        public_key.verify(signature, canonical)
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise UpdateCheckUnavailable("release-control signature verification failed") from exc
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise UpdateCheckUnavailable("release-control payload is not a mapping")
    return dict(payload)


def _parse_trust_store(
    trust_store: Mapping[str, object],
) -> dict[str, tuple[Ed25519PublicKey, datetime, datetime, bool]]:
    _require_exact_keys(trust_store, {"schema_version", "keys"}, "release trust store")
    if trust_store.get("schema_version") != 1:
        raise ValueError("release trust store schema is invalid")
    keys = trust_store.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= _MAX_TRUST_KEYS:
        raise ValueError("release trust store key set is invalid")
    parsed: dict[str, tuple[Ed25519PublicKey, datetime, datetime, bool]] = {}
    for item in keys:
        if not isinstance(item, dict):
            raise ValueError("release trust key is not a mapping")
        _require_exact_keys(
            item,
            {"key_id", "signature_algorithm", "public_key", "not_before", "not_after", "revoked"},
            "release trust key",
        )
        key_id = _required_text(item, "key_id", 96)
        if key_id in parsed or item.get("signature_algorithm") != "ed25519":
            raise ValueError("release trust key identity is invalid or duplicated")
        try:
            raw = base64.b64decode(_required_text(item, "public_key", 128), validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("release trust public key is invalid") from exc
        before = _parse_utc(_required_text(item, "not_before", 64), "not_before")
        after = _parse_utc(_required_text(item, "not_after", 64), "not_after")
        revoked = _required_bool(item, "revoked")
        if before >= after:
            raise ValueError("release trust key validity window is invalid")
        parsed[key_id] = (public_key, before, after, revoked)
    return parsed


def _validate_release(release: Mapping[str, object]) -> None:
    _require_exact_keys(
        release,
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
    if release.get("schema_version") != 1 or release.get("product") != "hermes-desktop":
        raise UpdateCheckUnavailable("Product Release schema/product is invalid")
    release_id = _required_text(release, "release_id", 160)
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise UpdateCheckUnavailable("Product Release identity is invalid")
    _required_text(release, "product_version", 64)
    _positive_int(release, "release_generation")
    _parse_utc(_required_text(release, "published_at", 64), "release published_at")
    if not isinstance(release.get("targets"), dict) or not release["targets"]:
        raise UpdateCheckUnavailable("Product Release target matrix is invalid")
    if not isinstance(release.get("components"), dict) or not isinstance(release.get("contracts"), dict):
        raise UpdateCheckUnavailable("Product Release component/contract matrix is invalid")


def _validate_channel(channel: Mapping[str, object], expected_channel: str) -> None:
    _require_exact_keys(
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
    if channel.get("schema_version") != 1 or channel.get("channel") != expected_channel:
        raise UpdateCheckUnavailable("signed Channel identity is invalid")
    _positive_int(channel, "channel_generation")
    _positive_int(channel, "release_generation")
    _parse_utc(_required_text(channel, "published_at", 64), "channel published_at")


def _validate_block(block: Mapping[str, object]) -> None:
    _require_exact_keys(
        block,
        {
            "schema_version",
            "block_generation",
            "published_at",
            "minimum_safe_release_generation",
            "blocked_releases",
        },
        "Block",
    )
    if block.get("schema_version") != 1:
        raise UpdateCheckUnavailable("signed Block schema is invalid")
    _positive_int(block, "block_generation")
    _parse_utc(_required_text(block, "published_at", 64), "block published_at")
    entries = _required_list(block, "blocked_releases")
    if len(entries) > 4096:
        raise UpdateCheckUnavailable("signed Block list is oversized")


def _artifact(kind: str, value: Mapping[str, object]) -> ReleaseArtifactRefV1:
    _require_exact_keys(value, {"object_key", "sha256", "size_bytes", "platform_signature"}, kind)
    object_key = _required_text(value, "object_key", 1024)
    sha256 = _required_text(value, "sha256", 64)
    size = _positive_int(value, "size_bytes")
    if _SHA256.fullmatch(sha256) is None or size > 8 * 1024 * 1024 * 1024:
        raise UpdateCheckUnavailable(f"signed {kind} artifact digest/size is invalid")
    parts = object_key.split("/")
    if (
        len(parts) != 6
        or parts[:3] != ["artifacts", "v1", "sha256"]
        or parts[3] != sha256[:2]
        or parts[4] != sha256
        or not parts[5]
        or any(part in {".", ".."} for part in parts)
    ):
        raise UpdateCheckUnavailable(f"signed {kind} object key is not content-addressed")
    return ReleaseArtifactRefV1(
        kind=kind,
        object_key=object_key,
        sha256=sha256,
        size_bytes=size,
    )


def _blocked_matches(entry: object, release_id: str, release_generation: int) -> bool:
    if not isinstance(entry, dict):
        raise UpdateCheckUnavailable("signed Block entry is not a mapping")
    _require_exact_keys(
        entry,
        {"release_id", "release_generation", "reason_code", "blocked_at"},
        "Block entry",
    )
    entry_id = _required_text(entry, "release_id", 160)
    entry_generation = _positive_int(entry, "release_generation")
    _required_text(entry, "reason_code", 96)
    _parse_utc(_required_text(entry, "blocked_at", 64), "blocked_at")
    return entry_id == release_id or entry_generation == release_generation


def _rollback_authorized(
    value: object,
    *,
    context: DeviceUpdateContextV1,
    target_release_id: str,
    target_generation: int,
    now: datetime,
) -> bool:
    if value is None:
        return False
    if not isinstance(value, dict):
        raise UpdateCheckUnavailable("signed rollback authorization is not a mapping")
    _require_exact_keys(
        value,
        {
            "from_release_id",
            "from_release_generation",
            "to_release_id",
            "to_release_generation",
            "reason_code",
            "expires_at",
        },
        "rollback authorization",
    )
    expires_at = _parse_utc(_required_text(value, "expires_at", 64), "rollback expires_at")
    _required_text(value, "reason_code", 96)
    return (
        context.active_release_id is not None
        and value.get("from_release_id") == context.active_release_id
        and value.get("from_release_generation") == context.active_release_generation
        and value.get("to_release_id") == target_release_id
        and value.get("to_release_generation") == target_generation
        and now < expires_at
    )


def _validate_rollout(values: Mapping[str, int]) -> dict[str, int]:
    if set(values) != _ALLOWED_CHANNELS:
        raise ValueError("rollout policy must define all release channels")
    result: dict[str, int] = {}
    for channel, value in values.items():
        if type(value) is not int or not 0 <= value <= 10_000:
            raise ValueError(f"rollout basis points are invalid for {channel}")
        result[channel] = value
    return result


def _earliest_time(first: datetime | None, second: datetime | None) -> datetime | None:
    values = [value for value in (first, second) if value is not None]
    return min(values) if values else None


def _optional_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UpdateCheckUnavailable("signed optional timestamp is invalid")
    return _parse_utc(value, "optional timestamp")


def _parse_utc(value: str, label: str) -> datetime:
    if not value.endswith("Z"):
        raise UpdateCheckUnavailable(f"{label} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise UpdateCheckUnavailable(f"{label} must be RFC3339 UTC") from exc
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise UpdateCheckUnavailable("catalog clock returned a naive timestamp")
    return value.astimezone(UTC)


def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise UpdateCheckUnavailable(f"{label} fields are invalid")


def _required_mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    item = value.get(name)
    if not isinstance(item, dict):
        raise UpdateCheckUnavailable(f"signed {name} is not a mapping")
    return dict(item)


def _required_list(value: Mapping[str, object], name: str) -> list[object]:
    item = value.get(name)
    if not isinstance(item, list):
        raise UpdateCheckUnavailable(f"signed {name} is not a list")
    return item


def _required_text(value: Mapping[str, object], name: str, maximum: int) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item or len(item) > maximum or "\x00" in item:
        raise UpdateCheckUnavailable(f"signed {name} is invalid")
    return item


def _required_bool(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if type(item) is not bool:
        raise UpdateCheckUnavailable(f"signed {name} is not boolean")
    return item


def _positive_int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int or item <= 0:
        raise UpdateCheckUnavailable(f"signed {name} must be a positive integer")
    return item


def _nonnegative_int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int or item < 0:
        raise UpdateCheckUnavailable(f"signed {name} must be a non-negative integer")
    return item
