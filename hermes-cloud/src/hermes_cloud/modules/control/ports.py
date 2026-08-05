from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
)


class ControlRuntimePort(Protocol):
    available_methods: tuple[str, ...]
    error_codes: Mapping[str, int]

    async def open(self, *, context: ControlRequestContext) -> None:
        """Attach one authenticated Cloud control transport."""

    async def execute(
        self,
        *,
        context: ControlRequestContext,
        method: str,
        params: dict[str, object],
    ) -> Mapping[str, object]:
        """Relay one ticket- and transport-bound request to the owner runtime."""

    async def close(
        self,
        *,
        context: ControlRequestContext,
        reason: str,
    ) -> None:
        """Detach one exact Cloud control transport and release in-memory state."""


class ControlRouteResolverPort(Protocol):
    async def resolve(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        """Resolve the authenticated session to one Connector route."""


class ControlRequestSenderPort(Protocol):
    async def send_control_request(
        self,
        request: Mapping[str, object],
    ) -> bool:
        """Return true only after the request crossed the effect boundary."""
