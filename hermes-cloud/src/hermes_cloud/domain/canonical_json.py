"""Canonical JSON digest primitives frozen by the root v1 contract."""

from __future__ import annotations

import hashlib
import json


def canonical_payload_digest(payload: object) -> str:
    """Return the v1 SHA-256 digest for one already-strictly-decoded payload."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
