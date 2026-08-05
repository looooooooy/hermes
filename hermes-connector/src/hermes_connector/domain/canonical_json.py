from __future__ import annotations

import json


class CanonicalJSONError(ValueError):
    """A value cannot be represented by the frozen canonical JSON profile."""


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise CanonicalJSONError("value is not canonical JSON") from None


__all__ = ["CanonicalJSONError", "canonical_json_bytes"]
