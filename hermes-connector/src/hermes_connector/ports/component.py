from __future__ import annotations

from typing import Protocol


class ComponentPort(Protocol):
    """Lifecycle boundary for a long-running supervised component.

    Implementations keep long-lived work inside ``run`` so the Supervisor's
    TaskGroup owns and deterministically joins or cancels that work.
    """

    @property
    def name(self) -> str:
        """Return the stable component identifier.

        Input/unit: none. Deadline: none; this is an in-memory read.
        Idempotency/effect: repeatable and side-effect free.
        Return: a non-empty process-local identifier. Errors: none expected.
        """

    async def start(self) -> None:
        """Prepare the component before its run loop starts.

        Input/unit: none. Deadline: the Supervisor start deadline in seconds.
        Idempotency: at most once unless the implementation documents otherwise.
        Effect: allocate component-local resources, but do not orphan background work.
        Return: ``None`` when prepared. Errors: component or deadline failures.
        """

    async def ready(self) -> bool:
        """Report whether startup reached a usable state.

        Input/unit: none. Deadline: the Supervisor start deadline in seconds.
        Idempotency/effect: repeatable readiness observation with no new effect.
        Return: ``True`` only when usable. Errors: component observation failures.
        """

    async def run(self) -> None:
        """Run long-lived work under Supervisor task ownership.

        Input/unit: none. Deadline: none while healthy; cancellation bounds lifetime.
        Idempotency: one run invocation per start. Effect: owns runtime I/O and tasks.
        Return: ``None`` after shutdown. Errors: runtime or cancellation failures.
        """

    async def drain(self) -> None:
        """Stop accepting new work and drain accepted work.

        Input/unit: none. Deadline: the Supervisor stop deadline in seconds.
        Idempotency: repeated drain requests must be safe.
        Effect: changes lifecycle state without claiming business completion.
        Return: ``None`` when draining begins/completes. Errors: drain failures.
        """

    async def stop(self) -> None:
        """Release component resources and finish owned work.

        Input/unit: none. Deadline: the Supervisor stop deadline in seconds.
        Idempotency: repeated cleanup must be safe.
        Effect: closes component I/O/resources. Return: ``None`` when stopped.
        Errors: cleanup or deadline failures.
        """
