"""Stable error categories safe for health and protocol responses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCategory(str, Enum):
    CONFIGURATION = "CONFIGURATION"
    LIFECYCLE = "LIFECYCLE"
    PROTOCOL = "PROTOCOL"
    DEPENDENCY = "DEPENDENCY"
    INTERNAL = "INTERNAL"


class CloudError(Exception):
    """Base exception with a stable public classification."""

    category = ErrorCategory.INTERNAL
    code = "INTERNAL_ERROR"
    retryable = False


@dataclass(frozen=True)
class ClassifiedError:
    category: ErrorCategory
    code: str
    retryable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "code": self.code,
            "retryable": self.retryable,
        }


def classify_error(error: BaseException) -> ClassifiedError:
    """Classify an exception without copying its text into public output."""

    if isinstance(error, CloudError):
        return ClassifiedError(error.category, error.code, error.retryable)
    return ClassifiedError(
        ErrorCategory.INTERNAL,
        "INTERNAL_ERROR",
        False,
    )
