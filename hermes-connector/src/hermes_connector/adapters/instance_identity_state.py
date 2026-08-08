"""Platform-neutral stable Connector instance identity state contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

MAX_INSTANCE_IDENTITY_BYTES = 4_096
_STATE_FIELDS = frozenset(
    {"version", "connector_instance_id", "client_instance_id"}
)


class UnsafeInstanceIdentity(ValueError):
    """The stable instance identity state is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class InstanceIdentities:
    connector_instance_id: UUID
    client_instance_id: UUID


def decode_instance_identities(raw: bytes) -> InstanceIdentities:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        raise UnsafeInstanceIdentity("instance identity content is invalid") from None
    if (
        not isinstance(value, dict)
        or frozenset(value) != _STATE_FIELDS
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    connector = _canonical_uuid(value.get("connector_instance_id"))
    client = _canonical_uuid(value.get("client_instance_id"))
    if connector == client:
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    return InstanceIdentities(connector, client)


def encode_instance_identities(identities: InstanceIdentities) -> bytes:
    if not isinstance(identities, InstanceIdentities):
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    if identities.connector_instance_id == identities.client_instance_id:
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    raw = json.dumps(
        {
            "client_instance_id": str(identities.client_instance_id),
            "connector_instance_id": str(identities.connector_instance_id),
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not 1 <= len(raw) <= MAX_INSTANCE_IDENTITY_BYTES:
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    return raw


def _canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise UnsafeInstanceIdentity("instance identity content is invalid") from None
    if str(parsed) != value:
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    return parsed


__all__ = [
    "InstanceIdentities",
    "MAX_INSTANCE_IDENTITY_BYTES",
    "UnsafeInstanceIdentity",
    "decode_instance_identities",
    "encode_instance_identities",
]
