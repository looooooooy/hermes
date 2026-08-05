"""Frozen Local Gateway profile identifier validation."""

from __future__ import annotations

import re

_PROFILE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def validate_profile(value: object) -> str:
    if not isinstance(value, str) or _PROFILE_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid local profile")
    return value


__all__ = ["validate_profile"]
