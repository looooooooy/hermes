"""Host-owned bounded dispatch for control owner actions."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

DEFAULT_OWNER_ACTION_MAX_WORKERS = 8
DEFAULT_OWNER_ACTION_MAX_QUEUED = 64
_STOP = object()


class BoundedOwnerActionDispatcher:
    """Bound process-wide owner work without blocking interpreter shutdown."""

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_OWNER_ACTION_MAX_WORKERS,
        max_queued: int = DEFAULT_OWNER_ACTION_MAX_QUEUED,
        thread_name_prefix: str = "control-owner-action",
    ) -> None:
        if max_workers <= 0 or max_queued < 0:
            raise ValueError("invalid_owner_action_capacity")
        self._permits = threading.BoundedSemaphore(max_workers + max_queued)
        self._queue: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._workers = tuple(
            threading.Thread(
                target=self._run,
                name=f"{thread_name_prefix}-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        )
        try:
            for worker in self._workers:
                worker.start()
        except BaseException:
            started_workers = tuple(
                worker for worker in self._workers if worker.ident is not None
            )
            for _worker in started_workers:
                self._queue.put(_STOP)
            for worker in started_workers:
                worker.join()
            raise

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any] | None:
        with self._lock:
            if self._closed or not self._permits.acquire(blocking=False):
                return None
            future: Future[Any] = Future()
            future.add_done_callback(lambda _future: self._permits.release())
            self._queue.put((future, fn, args, kwargs))
            return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        with self._lock:
            first_shutdown = not self._closed
            self._closed = True
        if first_shutdown:
            if cancel_futures:
                self._cancel_pending()
            for _worker in self._workers:
                self._queue.put(_STOP)
        if wait:
            for worker in self._workers:
                worker.join()

    def _cancel_pending(self) -> None:
        pending: list[object] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for item in pending:
            if item is _STOP:
                self._queue.put(item)
                continue
            future, _fn, _args, _kwargs = item
            future.cancel()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001
                future.set_exception(error)
            else:
                future.set_result(result)


__all__ = [
    "DEFAULT_OWNER_ACTION_MAX_QUEUED",
    "DEFAULT_OWNER_ACTION_MAX_WORKERS",
    "BoundedOwnerActionDispatcher",
]
