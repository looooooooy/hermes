from __future__ import annotations

from typing import Protocol

from hermes_connector.domain.local_gateway import (
    AgentEndpoint,
    LocalRuntimeAuthority,
)


class AgentDiscoveryPort(Protocol):
    async def discover(self, profile: str) -> tuple[AgentEndpoint, ...]:
        """Discover trusted Agent endpoints for one profile.

        Input/unit: profile identifier text. Deadline: caller's local RPC deadline
        in seconds. Idempotency/effect: repeatable bounded observation; never repairs
        or deletes descriptors. Return: deterministic tuple of trusted endpoints.
        Errors: cancellation or unexpected local I/O failures; invalid candidates
        are omitted.
        """

    async def aclose(self) -> None:
        """Close resources owned by endpoint discovery.

        Input/unit: none. Deadline: the Supervisor stop deadline in seconds.
        Idempotency: concurrent and repeated close calls are safe.
        Effect: rejects new discovery and joins any bounded discovery worker.
        Return: ``None`` after owned resources stop. Errors: cancellation is
        restored only after the cleanup barrier has completed.
        """


class LocalRuntimeAuthorityPort(Protocol):
    async def current_runtime_authority(self) -> LocalRuntimeAuthority | None:
        """Return the ready local Hermes authority or ``None``.

        The value is available only while the Local Gateway is ACTIVE. Callers
        must treat absence or a changed snapshot as a closed authority gate.
        """


class LocalGatewayConnectionPort(Protocol):
    async def exchange(self, frame: bytes) -> bytes:
        """Perform the connection's single Local Gateway request/response exchange.

        Input/unit: length-prefixed body bytes up to 262144.
        Deadline: caller's local RPC deadline in seconds. Idempotency: none; exactly
        one exchange per connection. Effect: one socket write and one socket read.
        Return: response body bytes. Errors: timeout, framing, size, connection,
        operating-system, or cancellation failures.
        """

    async def close(self) -> None:
        """Close one Local Gateway connection.

        Input/unit: none. Deadline: caller's local RPC/cleanup deadline in seconds.
        Idempotency: repeated close is safe. Effect: closes local socket resources.
        Return: ``None`` when closed. Errors: cancellation or OS cleanup failures.
        """


class LocalGatewayTransportPort(Protocol):
    async def connect(
        self,
        endpoint: AgentEndpoint,
    ) -> LocalGatewayConnectionPort:
        """Open a verified local transport connection.

        Input/unit: one trusted ``AgentEndpoint`` containing an absolute OS path.
        Deadline: caller's local-connect timeout in seconds. Idempotency: none; each
        call may open a socket. Effect: validates metadata and opens local I/O.
        Return: open single-exchange connection. Errors: trust, timeout, connection,
        operating-system, invalid-endpoint, or cancellation failures.
        """


class LocalSessionStatePort(Protocol):
    async def invalidate_runtime(
        self,
        previous_generation: str,
        current_generation: str,
    ) -> None:
        """Invalidate projections tied to a replaced Agent runtime.

        Input/unit: previous/current opaque generation identifiers.
        Deadline: caller's local RPC deadline in seconds. Idempotency key:
        ``(previous_generation, current_generation)``.
        Effect: invalidates only generation-scoped local session state.
        Return: ``None`` when durable/in-memory invalidation completes.
        Errors: storage, validation, timeout, or cancellation failures.
        """
