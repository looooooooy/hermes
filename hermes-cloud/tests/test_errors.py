from __future__ import annotations

from hermes_cloud.configuration import ConfigurationError
from hermes_cloud.errors import ErrorCategory, classify_error


def test_known_configuration_error_has_stable_safe_classification() -> None:
    classified = classify_error(ConfigurationError("unsafe detail"))

    assert classified.category is ErrorCategory.CONFIGURATION
    assert classified.as_dict() == {
        "category": "CONFIGURATION",
        "code": "INVALID_CONFIGURATION",
        "retryable": False,
    }


def test_unknown_error_does_not_expose_exception_text() -> None:
    secret = "unit-test-secret-value"

    classified = classify_error(RuntimeError(secret))

    assert classified.as_dict() == {
        "category": "INTERNAL",
        "code": "INTERNAL_ERROR",
        "retryable": False,
    }
    assert secret not in repr(classified.as_dict())
