from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from hermes_cloud.modules.cloud_api.domain import WebSocketTicketAuthentication


@dataclass(frozen=True, slots=True)
class ControlRequestContext:
    authentication: WebSocketTicketAuthentication
    connection_id: str

    def __post_init__(self) -> None:
        if str(UUID(self.connection_id)) != self.connection_id:
            raise ValueError("control connection id must be canonical UUID text")


@dataclass(frozen=True, slots=True)
class ControlConnectorRoute:
    """Authorized Connector address plus its Business tenant authority."""

    tenant_id: str
    device_id: str
    principal_tenant_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.device_id:
            raise ValueError("control connector route must be complete")
        if self.principal_tenant_id is not None:
            try:
                parsed = UUID(self.principal_tenant_id)
            except ValueError as error:
                raise ValueError(
                    "control route principal tenant must be canonical UUID text"
                ) from error
            if str(parsed) != self.principal_tenant_id:
                raise ValueError(
                    "control route principal tenant must be canonical UUID text"
                )


class ControlRpcError(RuntimeError):
    def __init__(self, *, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
