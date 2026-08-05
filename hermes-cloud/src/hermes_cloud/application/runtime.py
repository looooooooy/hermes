"""Lifecycle ownership and safe health projection for one component."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress

from hermes_cloud.application.dependencies import (
    DependencyCriticality,
    DependencyProbeResult,
    DependencyStatus,
)
from hermes_cloud.domain.lifecycle import ComponentLifecycle, LifecycleState
from hermes_cloud.errors import ClassifiedError, ErrorCategory, classify_error
from hermes_cloud.ports.dependency_probe import DependencyProbe

_DEPENDENCY_REFRESH_INTERVAL_SECONDS = 1.0


class ComponentRuntime:
    """Own startup, shutdown, readiness, and safe failure state."""

    def __init__(
        self,
        component: str,
        dependency_probes: Iterable[DependencyProbe] = (),
    ) -> None:
        self._lifecycle = ComponentLifecycle(component)
        self._dependency_probes = tuple(dependency_probes)
        for probe in self._dependency_probes:
            if probe.deadline_seconds <= 0:
                raise ValueError("dependency probe deadline must be positive")
        self._dependency_results: list[DependencyProbeResult] = []
        self._critical_dependency_failure_latched = False
        self._last_error: ClassifiedError | None = None
        self._dependency_monitor: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        self._lifecycle.transition(LifecycleState.STARTING)
        self._dependency_results = []
        self._critical_dependency_failure_latched = False
        self._last_error = None
        try:
            for probe in self._dependency_probes:
                result = await self._check_dependency(probe)
                self._dependency_results.append(result)
        except asyncio.CancelledError:
            self._last_error = ClassifiedError(
                ErrorCategory.LIFECYCLE,
                "STARTUP_CANCELLED",
                True,
            )
            self._lifecycle.transition(LifecycleState.FAILED)
            raise

        critical_failure = next(
            (
                result
                for result in self._dependency_results
                if result.criticality is DependencyCriticality.CRITICAL
                and not result.is_healthy
            ),
            None,
        )
        if critical_failure is not None:
            self._critical_dependency_failure_latched = True
            self._last_error = critical_failure.error
        self._lifecycle.transition(LifecycleState.READY)
        if self._dependency_probes:
            self._dependency_monitor = asyncio.create_task(self._monitor_dependencies())

    async def shutdown(self) -> None:
        dependency_monitor = self._dependency_monitor
        self._dependency_monitor = None
        if dependency_monitor is not None:
            if not dependency_monitor.done():
                dependency_monitor.cancel()
            with suppress(asyncio.CancelledError):
                await dependency_monitor
        self._lifecycle.transition(LifecycleState.STOPPING)
        self._lifecycle.transition(LifecycleState.STOPPED)

    def mark_failed(self, error: BaseException) -> None:
        self._last_error = classify_error(error)
        self._lifecycle.transition(LifecycleState.FAILED)

    def snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "component": self._lifecycle.component,
            "error": (None if self._last_error is None else self._last_error.as_dict()),
            "live": self._lifecycle.is_live,
            "ready": (
                self._lifecycle.is_ready and self._dependencies_allow_readiness()
            ),
            "state": self._lifecycle.state.value,
        }
        if self._dependency_probes:
            snapshot["dependencies"] = [
                result.as_dict() for result in self._dependency_results
            ]
            snapshot["diagnostic"] = self._dependency_diagnostic()
        return snapshot

    async def _check_dependency(
        self,
        probe: DependencyProbe,
    ) -> DependencyProbeResult:
        criticality = (
            DependencyCriticality.CRITICAL
            if probe.critical
            else DependencyCriticality.OPTIONAL
        )
        try:
            await asyncio.wait_for(
                probe.check(),
                timeout=probe.deadline_seconds,
            )
        except TimeoutError:
            return DependencyProbeResult(
                probe.name,
                criticality,
                DependencyStatus.TIMED_OUT,
                ClassifiedError(
                    ErrorCategory.DEPENDENCY,
                    "DEPENDENCY_TIMEOUT",
                    True,
                ),
            )
        except asyncio.CancelledError:
            raise
        # Dependency probes are injected adapters; any adapter failure must be
        # classified as unavailable without exposing implementation details.
        except Exception:  # noqa: BLE001
            return DependencyProbeResult(
                probe.name,
                criticality,
                DependencyStatus.FAILED,
                ClassifiedError(
                    ErrorCategory.DEPENDENCY,
                    "DEPENDENCY_UNAVAILABLE",
                    True,
                ),
            )
        return DependencyProbeResult(
            probe.name,
            criticality,
            DependencyStatus.HEALTHY,
        )

    async def _monitor_dependencies(self) -> None:
        while True:
            await asyncio.sleep(_DEPENDENCY_REFRESH_INTERVAL_SECONDS)
            round_has_critical_failure = False
            for index, probe in enumerate(self._dependency_probes):
                result = await self._check_dependency(probe)
                self._dependency_results[index] = result
                if (
                    result.criticality is DependencyCriticality.CRITICAL
                    and not result.is_healthy
                ):
                    round_has_critical_failure = True
                    self._critical_dependency_failure_latched = True
                    if self._lifecycle.state is LifecycleState.READY:
                        self._last_error = result.error
            if round_has_critical_failure:
                continue
            self._critical_dependency_failure_latched = False
            if self._lifecycle.state is LifecycleState.READY:
                self._last_error = None

    def _dependencies_allow_readiness(self) -> bool:
        return (
            not self._critical_dependency_failure_latched
            and len(self._dependency_results) == len(self._dependency_probes)
            and all(
                result.criticality is not DependencyCriticality.CRITICAL
                or result.is_healthy
                for result in self._dependency_results
            )
        )

    def _dependency_diagnostic(self) -> str:
        if len(self._dependency_results) < len(self._dependency_probes):
            return "CHECKING"
        if self._critical_dependency_failure_latched or any(
            (
                result.criticality is DependencyCriticality.CRITICAL
                and not result.is_healthy
            )
            for result in self._dependency_results
        ):
            return "BLOCKED"
        if any(not result.is_healthy for result in self._dependency_results):
            return "DEGRADED"
        return "HEALTHY"
