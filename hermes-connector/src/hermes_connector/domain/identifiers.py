"""Strict reusable identifier validation for Connector authority boundaries."""

from __future__ import annotations

from uuid import RFC_4122, UUID


def canonical_uuid(value: object) -> UUID:
    """Return a canonical non-nil RFC 4122 UUID using versions 1 through 5."""

    if isinstance(value, UUID):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError("UUID is invalid") from None
        if str(parsed) != value:
            raise ValueError("UUID is not canonical")
    else:
        raise TypeError("UUID is invalid")
    if (
        parsed.int == 0
        or parsed.variant != RFC_4122
        or parsed.version not in {1, 2, 3, 4, 5}
    ):
        raise ValueError("UUID is outside the supported RFC 4122 profile")
    return parsed
