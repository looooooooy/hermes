"""Stable, body-free errors defined by the core contract packet."""

from __future__ import annotations

_ERRORS: dict[str, tuple[int, bool, str]] = {
    "contract_unsupported": (4300, False, "contract version is unsupported"),
    "invalid_envelope": (4301, False, "envelope is invalid"),
    "frame_too_large": (4302, False, "frame exceeds the contract limit"),
    "invalid_utf8": (4303, False, "frame is not valid UTF-8"),
    "capability_not_available": (
        4304,
        False,
        "required capability is unavailable",
    ),
}


class CoreContractError(ValueError):
    """Reject contract input without retaining or rendering its body."""

    def __init__(self, category: str) -> None:
        code, retryable, message = _ERRORS[category]
        self.category = category
        self.code = code
        self.retryable = retryable
        super().__init__(message)
