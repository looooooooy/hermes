"""Cancellation-safe async pairing command lock for Windows."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import TracebackType
from typing import Self

from .instance_lock import AlreadyRunning, WindowsInstanceLock

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.05


class PairingCommandLockTimeout(RuntimeError):
    """The pairing command lock was not available before its deadline."""


class WindowsPairingCommandLock:
    """Hold one cross-process Windows byte-range lock for one pairing command."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not isinstance(poll_interval_seconds, int | float)
            or isinstance(poll_interval_seconds, bool)
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("pairing command lock timing is invalid")
        self._lock = WindowsInstanceLock(path)
        self._timeout_seconds = float(timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)

    async def __aenter__(self) -> Self:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        try:
            while True:
                try:
                    self._lock.acquire()
                except AlreadyRunning:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise PairingCommandLockTimeout(
                            "pairing command lock timed out"
                        ) from None
                    await asyncio.sleep(min(self._poll_interval_seconds, remaining))
                else:
                    return self
        except asyncio.CancelledError:
            self._lock.close()
            raise
        except BaseException:
            self._lock.close()
            raise

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self._lock.close()


__all__ = ["PairingCommandLockTimeout", "WindowsPairingCommandLock"]
