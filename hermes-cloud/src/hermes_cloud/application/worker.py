"""Async Worker lifecycle runner without infrastructure dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from hermes_cloud.application.runtime import ComponentRuntime
from hermes_cloud.ports.dependency_probe import DependencyProbe


class WorkerRunner:
    """Start, wait, and stop the initial no-dependency worker process."""

    def __init__(
        self,
        dependency_probes: Iterable[DependencyProbe] = (),
    ) -> None:
        self._runtime = ComponentRuntime("async-worker", dependency_probes)

    async def start(self) -> None:
        await self._runtime.startup()

    async def stop(self) -> None:
        await self._runtime.shutdown()

    async def run(self, stop_event: asyncio.Event) -> None:
        await self.start()
        try:
            await stop_event.wait()
        finally:
            await self.stop()

    def snapshot(self) -> dict[str, object]:
        return self._runtime.snapshot()
