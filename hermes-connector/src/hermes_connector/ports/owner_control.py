from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from hermes_connector.domain.owner_control import (
    OwnerControlRequest,
    OwnerControlResponse,
)


class OwnerControlScopePort(Protocol):
    control_transport_id: UUID
    principal_id: str
    client_instance_id: UUID
    session_key: str
    profile: str


class OwnerControlChannelPort(Protocol):
    async def execute(
        self,
        *,
        operation: str,
        request_id: UUID,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class OwnerControlChannelFactoryPort(Protocol):
    async def open(
        self,
        *,
        scope: OwnerControlScopePort,
        request_id: UUID,
        timeout_seconds: float,
    ) -> OwnerControlChannelPort: ...


class OwnerControlLanePort(Protocol):
    async def process(self, request: OwnerControlRequest) -> OwnerControlResponse: ...

    async def close_all(self) -> None: ...
