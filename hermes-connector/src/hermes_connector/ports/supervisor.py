from __future__ import annotations

from typing import Protocol


class SupervisorPort(Protocol):
    async def start(self) -> None:
        """Start owned components and wait for readiness.

        Input/unit: none. Deadline: configured Supervisor start deadline in seconds.
        Idempotency: one successful invocation per Supervisor instance.
        Effect: starts lifecycle-owned component tasks. Return: ``None`` when ready.
        Errors: startup, readiness, deadline, component, or cancellation failures.
        """

    async def wait(self) -> None:
        """Wait for the supervised runtime to stop or fail after readiness.

        Input/unit: none. Deadline: none while healthy; component failure completes
        the wait. Idempotency/effect: repeatable observation with no new lifecycle
        effect. Return: ``None`` after a normal stop. Errors: safe runtime failure
        category or cancellation.
        """

    async def stop(self) -> None:
        """Drain and stop all lifecycle-owned components.

        Input/unit: none. Deadline: configured Supervisor stop deadline in seconds.
        Idempotency: repeated stop after completion is safe.
        Effect: drains components, closes resources, and joins owned tasks.
        Return: ``None`` when stopped. Errors: stop, deadline, component, or
        cancellation failures.
        """
