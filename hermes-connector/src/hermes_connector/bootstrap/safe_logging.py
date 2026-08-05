from __future__ import annotations

import json
import re
from collections.abc import Callable

from hermes_connector.ports.logging import LogCategory, LogState

_SAFE_COMPONENT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class SafeStructuredLogger:
    """Emit a fixed, payload-free lifecycle event shape."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def emit(
        self,
        *,
        category: LogCategory,
        component: str,
        state: LogState,
    ) -> None:
        if not isinstance(category, LogCategory):
            raise TypeError("category must be a LogCategory")
        if not isinstance(state, LogState):
            raise TypeError("state must be a LogState")
        if _SAFE_COMPONENT.fullmatch(component) is None:
            raise ValueError("component must be a safe static identifier")

        self._sink(
            json.dumps(
                {
                    "category": category.value,
                    "component": component,
                    "state": state.value,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )


__all__ = [
    "LogCategory",
    "LogState",
    "SafeStructuredLogger",
]
