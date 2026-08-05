"""Ports consumed by the lifecycle application service."""

from __future__ import annotations

from typing import Any, Protocol


class LifecycleResourcePort(Protocol):
    """A bounded resource owned by one Local Gateway runtime generation.

    The application registers a start attempt before invoking ``start``.
    Therefore ``stop`` must be idempotent and safe after a partial or failed
    ``start`` as well as after a successful start.
    """

    name: str

    def start(self, deadline: float) -> None: ...

    def drain(self, deadline: float) -> None: ...

    def stop(self, deadline: float) -> None: ...


class LocalHandshakePort(Protocol):
    """Contract adapter used only while its runtime generation is READY."""

    def handle_hello(self, raw: Any) -> str: ...
