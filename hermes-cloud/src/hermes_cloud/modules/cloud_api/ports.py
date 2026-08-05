"""Infrastructure-neutral dependency ports for the external Cloud P0 surface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Protocol
from uuid import UUID

from hermes_cloud.modules.cloud_api.domain import (
    ObserverSubscription,
    Principal,
)


class SecretResolverPort(Protocol):
    def resolve(self, reference: str) -> bytes: ...


class LoginTenantResolverPort(Protocol):
    def tenant_for_subject(self, subject: str) -> UUID | None: ...


class ProjectionEventSourcePort(Protocol):
    def events(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: str | None,
        after_sequence: int,
        agent_id: UUID | None = None,
    ) -> AsyncIterator[Mapping[str, object]]: ...


class ObserverSubscriptionPort(Protocol):
    def open_subscription(
        self,
        *,
        principal: Principal,
        session_key: str,
        profile: str | None,
        agent_id: UUID | None = None,
    ) -> ObserverSubscription: ...

    def snapshot_ready(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
    ) -> bool: ...

    def renew_subscription(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
    ) -> None: ...

    def close_subscription(
        self,
        *,
        principal: Principal,
        subscription_id: UUID,
        reason: str,
    ) -> None: ...
