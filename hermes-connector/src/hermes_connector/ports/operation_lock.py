"""Cross-process operation serialization boundary."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self


class OperationLockPort(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
