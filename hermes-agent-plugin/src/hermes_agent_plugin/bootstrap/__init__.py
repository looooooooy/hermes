"""Production bootstrap for registration with the running Hermes Agent."""

from __future__ import annotations

from typing import Any

__all__ = ["register"]


def __getattr__(name: str) -> Any:
    if name == "register":
        from .registration import register

        value = register
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
