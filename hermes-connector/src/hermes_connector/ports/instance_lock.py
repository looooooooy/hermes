from __future__ import annotations

from typing import Protocol


class InstanceLockPort(Protocol):
    def acquire(self) -> None:
        """Acquire the single-process Connector instance lock.

        Input/unit: none; the adapter owns one configured lock-file path.
        Deadline: non-blocking acquisition with no retry wait.
        Idempotency: repeated acquire by the same holder is safe.
        Effect: opens and holds one OS process lock. Return: ``None`` when held.
        Errors: already-running, unsafe-path, permission, or OS lock failures.
        """

    def close(self) -> None:
        """Release the Connector instance lock and descriptor.

        Input/unit: none. Deadline: synchronous local cleanup without network I/O.
        Idempotency: repeated close is safe. Effect: releases the OS lock while
        retaining the dedicated lock file. Return: ``None``. Errors: OS close errors.
        """
