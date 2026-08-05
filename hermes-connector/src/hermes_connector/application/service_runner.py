from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from hermes_connector.ports.instance_lock import InstanceLockPort
from hermes_connector.ports.supervisor import SupervisorPort


class ServiceState(StrEnum):
    NEW = "new"
    LOCKED = "locked"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class InvalidServiceTransition(ValueError):
    def __init__(self, source: ServiceState, target: ServiceState) -> None:
        super().__init__(
            f"service transition not allowed: {source.value} -> {target.value}"
        )
        self.source = source
        self.target = target


# NEW --acquire lock--> LOCKED --Supervisor.start--> RUNNING
#  |                       |                         |
#  | acquire failure       | start failure           | stop requested
#  v                       v                         v
# FAILED <-----------------+                    STOPPING
#    ^                                               |
#    | Supervisor.stop or release failure            | stop then release
#    +-----------------------------------------------+v
#                                                 STOPPED
#
# Invariants:
# - Supervisor.start is attempted only while the process lock is held.
# - Supervisor.stop is attempted before releasing a held process lock.
# - STOPPED and FAILED are terminal; cleanup is idempotent after either state.
SERVICE_TRANSITIONS: Final[Mapping[ServiceState, frozenset[ServiceState]]] = (
    MappingProxyType(
        {
            ServiceState.NEW: frozenset(
                {
                    ServiceState.LOCKED,
                    ServiceState.FAILED,
                }
            ),
            ServiceState.LOCKED: frozenset(
                {
                    ServiceState.RUNNING,
                    ServiceState.FAILED,
                }
            ),
            ServiceState.RUNNING: frozenset(
                {
                    ServiceState.STOPPING,
                    ServiceState.FAILED,
                }
            ),
            ServiceState.STOPPING: frozenset(
                {
                    ServiceState.STOPPED,
                    ServiceState.FAILED,
                }
            ),
            ServiceState.STOPPED: frozenset(),
            ServiceState.FAILED: frozenset(),
        }
    )
)


def transition_service(source: ServiceState, target: ServiceState) -> ServiceState:
    if target not in SERVICE_TRANSITIONS[source]:
        raise InvalidServiceTransition(source, target)
    return target


class ServiceRunner:
    def __init__(
        self,
        instance_lock: InstanceLockPort,
        supervisor: SupervisorPort,
    ) -> None:
        self._instance_lock = instance_lock
        self._supervisor = supervisor
        self._state = ServiceState.NEW
        self._lock_held = False

    @property
    def state(self) -> ServiceState:
        return self._state

    async def start(self) -> None:
        if self._state is not ServiceState.NEW:
            raise RuntimeError("service runner can only be started once")

        try:
            self._instance_lock.acquire()
        except BaseException:
            with suppress(BaseException):
                self._instance_lock.close()
            self._state = transition_service(self._state, ServiceState.FAILED)
            raise

        self._lock_held = True
        self._state = transition_service(self._state, ServiceState.LOCKED)
        try:
            await self._supervisor.start()
        except BaseException:
            with suppress(BaseException):
                await self._supervisor.stop()
            self._release_lock_safely()
            self._state = transition_service(self._state, ServiceState.FAILED)
            raise

        self._state = transition_service(self._state, ServiceState.RUNNING)

    async def stop(self) -> None:
        if self._state in {ServiceState.STOPPED, ServiceState.FAILED}:
            return
        if self._state is not ServiceState.RUNNING:
            raise RuntimeError("service runner is not running")

        self._state = transition_service(self._state, ServiceState.STOPPING)
        try:
            await self._supervisor.stop()
            self._release_lock()
        except BaseException:
            self._release_lock_safely()
            self._state = transition_service(self._state, ServiceState.FAILED)
            raise

        self._state = transition_service(self._state, ServiceState.STOPPED)

    async def run_until(self, stop_event: asyncio.Event) -> None:
        await self.start()
        stop_wait = asyncio.create_task(
            stop_event.wait(),
            name="hermes-connector:stop-signal",
        )
        supervisor_wait = asyncio.create_task(
            self._supervisor.wait(),
            name="hermes-connector:supervisor-wait",
        )
        try:
            done, _ = await asyncio.wait(
                (stop_wait, supervisor_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if supervisor_wait in done:
                try:
                    await supervisor_wait
                except BaseException:
                    self._release_lock_safely()
                    self._state = transition_service(
                        self._state,
                        ServiceState.FAILED,
                    )
                    raise
                self._release_lock_safely()
                self._state = transition_service(
                    self._state,
                    ServiceState.FAILED,
                )
                raise RuntimeError("supervisor stopped unexpectedly")
        finally:
            for waiter in (stop_wait, supervisor_wait):
                if not waiter.done():
                    waiter.cancel()
            for waiter in (stop_wait, supervisor_wait):
                with suppress(BaseException):
                    await waiter
            if self._state is ServiceState.RUNNING:
                await self.stop()

    def _release_lock(self) -> None:
        if not self._lock_held:
            return
        self._instance_lock.close()
        self._lock_held = False

    def _release_lock_safely(self) -> None:
        with suppress(BaseException):
            self._release_lock()
