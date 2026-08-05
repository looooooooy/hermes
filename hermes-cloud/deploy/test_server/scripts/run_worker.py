"""Signal-aware deployment runner for the package worker entrypoint."""

from __future__ import annotations

import asyncio
import signal

from hermes_cloud.entrypoints.worker import create_worker


async def _run() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop_event.set)
    await create_worker().run(stop_event)


if __name__ == "__main__":
    asyncio.run(_run())
