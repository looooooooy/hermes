"""Foundation-only local projection behavior for the macOS runtime."""

from __future__ import annotations


class FoundationNoOpLocalProjectionInvalidator:
    """Explicitly keep zero business projection state in the Foundation slice."""

    __slots__ = ()
    foundation_effect = "none"

    async def invalidate_runtime(
        self,
        previous_generation: str,
        current_generation: str,
    ) -> None:
        del previous_generation, current_generation
