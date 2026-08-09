"""Authenticated onboarding context for Desktop device pairing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hermes_cloud.modules.cloud_api.domain import Principal


@dataclass(frozen=True, slots=True)
class PairingTarget:
    workspace_id: str
    workspace_key: str
    workspace_display_name: str
    agent_id: str
    agent_key: str


class PairingContextResolverPort(Protocol):
    def targets_for(self, principal: Principal) -> tuple[PairingTarget, ...]: ...


__all__ = ["PairingContextResolverPort", "PairingTarget"]
