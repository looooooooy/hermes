# ruff: noqa: BLE001, S110
"""Exception-safe lifecycle helpers for macOS relay servers."""

from __future__ import annotations

import os
import threading
from typing import Any


def _force_server_shutdown(server: Any) -> None:
    server_socket = getattr(server, "socket", None)
    close_socket = getattr(server_socket, "close", None)
    if callable(close_socket):
        try:
            close_socket()
        except BaseException:
            pass

    notifier = getattr(server, "shutdown_notifier", None)
    if isinstance(notifier, int):
        try:
            os.write(notifier, b"x")
        except BaseException:
            pass


def shutdown_server(server: Any) -> BaseException | None:
    """Request shutdown once and force-close the selector resources on error."""

    try:
        server.shutdown()
    except BaseException as error:
        _force_server_shutdown(server)
        return error
    return None


def shutdown_server_and_join(
    server: Any,
    thread: threading.Thread,
    *,
    attempts: int,
    join_timeout_s: float = 2.0,
) -> tuple[bool, BaseException | None]:
    """Request shutdown, force-wake the selector, and report retryability."""

    first_error: BaseException | None = None
    for _attempt in range(max(1, attempts)):
        shutdown_error = shutdown_server(server)
        first_error = first_error or shutdown_error

        if threading.current_thread() is not thread:
            try:
                if thread.is_alive():
                    thread.join(timeout=join_timeout_s)
            except BaseException as error:
                first_error = first_error or error
        if not thread.is_alive():
            return True, first_error

    return False, first_error


__all__ = ["shutdown_server", "shutdown_server_and_join"]
