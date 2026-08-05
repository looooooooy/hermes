"""Application service coordinating Local Gateway lifecycle ports."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from typing import Any

from ..domain.lifecycle import (
    ALLOWED_LIFECYCLE_TRANSITIONS,
    GatewayState,
    LifecycleCancelled,
    LifecycleDeadlineExceeded,
    LifecycleNotReady,
    LifecycleTransitionError,
)
from ..ports.lifecycle import LifecycleResourcePort, LocalHandshakePort

_DEFAULT_TIMEOUT_S = 3.0
_MAX_TIMEOUT_S = 30.0
_log = logging.getLogger(__name__)


class GatewayLifecycle:
    """Own one installed gateway and its restartable runtime generations."""

    def __init__(
        self,
        *,
        resources: Iterable[LifecycleResourcePort],
        adapter_factory: Callable[[str], LocalHandshakePort],
        generation_factory: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
        default_timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._resources = tuple(resources)
        self._adapter_factory = adapter_factory
        self._generation_factory = generation_factory
        self._clock = clock
        self._default_timeout_s = default_timeout_s
        self._state = GatewayState.NEW
        self._started: list[LifecycleResourcePort] = []
        self._runtime_generation: str | None = None
        self._local_contract: LocalHandshakePort | None = None

    @property
    def state(self) -> GatewayState:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state is GatewayState.READY

    @property
    def runtime_generation(self) -> str | None:
        return self._runtime_generation

    def _transition(self, target: GatewayState) -> None:
        if target not in ALLOWED_LIFECYCLE_TRANSITIONS[self._state]:
            raise LifecycleTransitionError("lifecycle_transition_not_allowed")
        self._state = target

    def _deadline(self, timeout_s: float | None) -> float:
        timeout = self._default_timeout_s if timeout_s is None else timeout_s
        if isinstance(timeout, bool):
            raise LifecycleDeadlineExceeded("lifecycle_deadline_invalid")
        try:
            seconds = float(timeout)
        except (TypeError, ValueError):
            raise LifecycleDeadlineExceeded("lifecycle_deadline_invalid") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise LifecycleDeadlineExceeded("lifecycle_deadline_invalid")
        return self._clock() + min(seconds, _MAX_TIMEOUT_S)

    def _raise_if_interrupted(
        self,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if cancelled is not None and cancelled():
            raise LifecycleCancelled("lifecycle_cancelled")
        if self._clock() >= deadline:
            raise LifecycleDeadlineExceeded("lifecycle_deadline_exceeded")

    def _clear_runtime(self) -> None:
        self._started.clear()
        self._local_contract = None
        self._runtime_generation = None

    def _rollback_start(self) -> None:
        self._transition(GatewayState.STOPPING)
        cleanup_deadline = self._deadline(None)
        for resource in reversed(self._started):
            try:
                resource.stop(cleanup_deadline)
            except BaseException:  # noqa: BLE001
                _log.warning("gateway resource cleanup failed")
        self._clear_runtime()
        self._transition(GatewayState.STOPPED)

    def install(self) -> None:
        if self._state is GatewayState.NEW:
            self._transition(GatewayState.INSTALLED)

    def start(
        self,
        *,
        timeout_s: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        if self._state is GatewayState.READY:
            assert self._runtime_generation is not None
            return self._runtime_generation
        deadline = self._deadline(timeout_s)
        self._transition(GatewayState.STARTING)
        try:
            self._raise_if_interrupted(deadline, cancelled)
            generation = self._generation_factory()
            self._local_contract = self._adapter_factory(generation)
            self._runtime_generation = generation
            for resource in self._resources:
                self._raise_if_interrupted(deadline, cancelled)
                self._started.append(resource)
                resource.start(deadline)
                self._raise_if_interrupted(deadline, cancelled)
            self._transition(GatewayState.READY)
            return generation
        except BaseException:
            self._rollback_start()
            raise

    def drain(self, *, timeout_s: float | None = None) -> None:
        if self._state is not GatewayState.READY:
            return
        deadline = self._deadline(timeout_s)
        self._transition(GatewayState.DRAINING)
        first_error: BaseException | None = None
        for resource in reversed(self._started):
            try:
                resource.drain(deadline)
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def stop(self, *, timeout_s: float | None = None) -> None:
        if self._state in {GatewayState.NEW, GatewayState.STOPPED}:
            return
        deadline = self._deadline(timeout_s)
        first_error: BaseException | None = None
        if self._state is GatewayState.READY:
            try:
                self.drain(timeout_s=timeout_s)
            except BaseException as error:  # noqa: BLE001
                first_error = error
        self._transition(GatewayState.STOPPING)
        for resource in reversed(self._started):
            try:
                resource.stop(deadline)
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
        self._clear_runtime()
        self._transition(GatewayState.STOPPED)
        if first_error is not None:
            raise first_error

    def handle_local_hello(self, raw: Any) -> str:
        if self._state is not GatewayState.READY or self._local_contract is None:
            raise LifecycleNotReady("gateway_not_ready")
        return self._local_contract.handle_hello(raw)
