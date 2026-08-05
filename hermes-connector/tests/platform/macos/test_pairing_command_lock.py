from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time
from pathlib import Path

import pytest

from hermes_connector.adapters.platform.macos.instance_lock import (
    AlreadyRunning,
    MacOSInstanceLock,
)
from hermes_connector.adapters.platform.macos.pairing_command_lock import (
    MacOSPairingCommandLock,
    PairingCommandLockTimeout,
)


def _hold_lock_forever(path: str, ready: multiprocessing.synchronize.Event) -> None:
    lock = MacOSInstanceLock(path)
    lock.acquire()
    ready.set()
    while True:
        time.sleep(1)


def _start_permanent_holder(path: Path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_hold_lock_forever,
        args=(str(path), ready),
    )
    process.start()
    assert ready.wait(timeout=3)
    return process


def _wait_for_lock_until_sigint(
    path: str,
    ready: multiprocessing.synchronize.Event,
) -> None:
    async def wait_for_lock() -> None:
        ready.set()
        async with MacOSPairingCommandLock(
            path,
            timeout_seconds=10,
            poll_interval_seconds=0.01,
        ):
            raise AssertionError("holder must retain the lock")

    try:
        asyncio.run(wait_for_lock())
    except KeyboardInterrupt:
        raise SystemExit(130) from None


@pytest.mark.asyncio
async def test_permanently_held_pairing_lock_times_out_without_background_waiter(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "pairing-command.lock"
    holder = _start_permanent_holder(path)
    try:
        started = time.monotonic()
        with pytest.raises(PairingCommandLockTimeout):
            async with MacOSPairingCommandLock(
                path,
                timeout_seconds=0.05,
                poll_interval_seconds=0.005,
            ):
                raise AssertionError("unreachable")
        assert time.monotonic() - started < 0.5
        assert holder.is_alive()
    finally:
        holder.terminate()
        holder.join(timeout=3)

    recovered = MacOSInstanceLock(path)
    recovered.acquire()
    await asyncio.sleep(0.05)
    competitor = MacOSInstanceLock(path)
    with pytest.raises(AlreadyRunning):
        competitor.acquire()
    recovered.close()


@pytest.mark.asyncio
async def test_cancelled_pairing_lock_wait_returns_immediately_and_never_acquires_later(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "pairing-command.lock"
    holder = _start_permanent_holder(path)
    waiter = asyncio.create_task(
        MacOSPairingCommandLock(
            path,
            timeout_seconds=10,
            poll_interval_seconds=0.01,
        ).__aenter__()
    )
    try:
        await asyncio.sleep(0.03)
        started = time.monotonic()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert time.monotonic() - started < 0.2
        assert holder.is_alive()
    finally:
        holder.terminate()
        holder.join(timeout=3)

    recovered = MacOSInstanceLock(path)
    recovered.acquire()
    await asyncio.sleep(0.05)
    competitor = MacOSInstanceLock(path)
    with pytest.raises(AlreadyRunning):
        competitor.acquire()
    recovered.close()


@pytest.mark.asyncio
async def test_sigint_style_task_cancellation_does_not_wait_for_lock_owner_exit(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "pairing-command.lock"
    holder = _start_permanent_holder(path)

    async def command() -> None:
        async with MacOSPairingCommandLock(
            path,
            timeout_seconds=10,
            poll_interval_seconds=0.01,
        ):
            raise AssertionError("unreachable")

    task = asyncio.create_task(command())
    try:
        await asyncio.sleep(0.03)
        task.cancel("SIGINT")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        assert holder.is_alive()
    finally:
        holder.terminate()
        holder.join(timeout=3)


def test_real_sigint_exits_bounded_lock_wait_without_late_acquisition(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "pairing-command.lock"
    owner = MacOSInstanceLock(path)
    owner.acquire()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    waiter = context.Process(
        target=_wait_for_lock_until_sigint,
        args=(str(path), ready),
    )
    waiter.start()
    try:
        assert ready.wait(timeout=3)
        os.kill(waiter.pid, signal.SIGINT)
        waiter.join(timeout=1)
        assert not waiter.is_alive()
        assert waiter.exitcode == 130
        competitor = MacOSInstanceLock(path)
        with pytest.raises(AlreadyRunning):
            competitor.acquire()
    finally:
        if waiter.is_alive():
            waiter.kill()
            waiter.join(timeout=3)
        owner.close()
