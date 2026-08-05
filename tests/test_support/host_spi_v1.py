"""Test-only opaque Host return values for cross-module E2E harnesses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Literal


@dataclass(frozen=True, repr=False)
class TestOwnerActionResult:
    __test__: ClassVar[bool] = False

    status: Literal["accepted", "rejected", "effect_unknown"]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


__all__ = ["TestOwnerActionResult"]
