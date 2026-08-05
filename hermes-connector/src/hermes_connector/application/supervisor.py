from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TypeVar

from hermes_connector.domain.health import (
    ComponentSnapshot,
    HealthSnapshot,
    SupervisorPhase,
)
from hermes_connector.ports.component import ComponentPort
from hermes_connector.ports.configuration import SupervisorConfigPort
from hermes_connector.ports.logging import LogCategory, LogState, SafeLogPort


class SupervisorStartError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "startup_failure",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class SupervisorStopError(RuntimeError):
    pass


class SupervisorRuntimeError(RuntimeError):
    pass


class _ComponentNotReady(RuntimeError):
    pass


class _ComponentFailure(RuntimeError):
    def __init__(
        self,
        *,
        category: str = "component_failure",
        retryable: bool = False,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable


class _StopDuringStartup(RuntimeError):
    pass


class _CleanupFailed(RuntimeError):
    pass


class _StartDeadline(TimeoutError):
    pass


class _CleanupDeadline(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class _CleanupResult:
    failed: bool
    deadline_exceeded: bool


ResultT = TypeVar("ResultT")


class Supervisor:
    """Own component lifetimes and expose cached, side-effect-free health."""

    def __init__(
        self,
        components: Iterable[ComponentPort],
        config: SupervisorConfigPort,
        logger: SafeLogPort,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._components = tuple(components)
        names = [component.name for component in self._components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique")

        self._config = config
        self._logger = logger
        self._clock = clock
        self._phase = SupervisorPhase.NEW
        self._component_states = {
            component.name: LogState.STOPPED for component in self._components
        }
        self._failure_category: str | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._startup_result: asyncio.Future[None] | None = None
        self._stop_requested: asyncio.Event | None = None
        self._stop_deadline_at: float | None = None
        self._owned_tasks: set[asyncio.Task[object]] = set()

    @property
    def owned_task_count(self) -> int:
        return sum(not task.done() for task in self._owned_tasks)

    def snapshot(self) -> HealthSnapshot:
        return HealthSnapshot(
            live=self._phase not in {SupervisorPhase.STOPPED, SupervisorPhase.FAILED},
            ready=self._phase is SupervisorPhase.READY,
            phase=self._phase,
            components=tuple(
                ComponentSnapshot(name=name, state=self._component_states[name].value)
                for name in sorted(self._component_states)
            ),
            failure_category=self._failure_category,
        )

    async def start(self) -> None:
        if self._phase is not SupervisorPhase.NEW:
            raise SupervisorStartError("supervisor can only be started once")

        loop = asyncio.get_running_loop()
        self._phase = SupervisorPhase.STARTING
        self._startup_result = loop.create_future()
        self._startup_result.add_done_callback(self._observe_future)
        self._stop_requested = asyncio.Event()
        lifecycle_task = asyncio.create_task(
            self._run_lifecycle(),
            name="hermes-connector:supervisor",
        )
        self._lifecycle_task = lifecycle_task
        self._track_task(lifecycle_task)

        try:
            await asyncio.shield(self._startup_result)
        except asyncio.CancelledError:
            raise
        except SupervisorStartError:
            await asyncio.shield(lifecycle_task)
            raise

    async def stop(self) -> None:
        if self._phase is SupervisorPhase.NEW:
            self._phase = SupervisorPhase.STOPPED
            return
        if self._stop_requested is not None:
            if not self._stop_requested.is_set():
                self._stop_deadline_at = (
                    self._now() + self._config.stop_deadline_seconds
                )
            self._stop_requested.set()
        if self._lifecycle_task is not None:
            await asyncio.shield(self._lifecycle_task)
        if self._phase is SupervisorPhase.FAILED:
            raise SupervisorStopError(
                f"supervisor stop failed: {self._failure_category or 'failure'}"
            )

    async def wait(self) -> None:
        lifecycle_task = self._lifecycle_task
        if lifecycle_task is None:
            raise SupervisorRuntimeError("supervisor runtime is not started")
        await asyncio.shield(lifecycle_task)
        if self._phase is SupervisorPhase.FAILED:
            raise SupervisorRuntimeError(
                f"supervisor runtime failed: {self._failure_category or 'failure'}"
            )

    async def _run_lifecycle(self) -> None:
        started: list[ComponentPort] = []
        runners: list[asyncio.Task[None]] = []
        operations: list[asyncio.Task[object]] = []
        reached_ready = False
        cleanup_task: asyncio.Task[_CleanupResult] | None = None
        shutdown_deadline: float | None = None
        self._log_supervisor(LogState.STARTING)

        def require_shutdown_deadline() -> float:
            nonlocal shutdown_deadline
            if shutdown_deadline is None:
                shutdown_deadline = self._stop_deadline_at
                if shutdown_deadline is None:
                    shutdown_deadline = self._now() + self._config.stop_deadline_seconds
            return shutdown_deadline

        def cleanup_once() -> asyncio.Task[_CleanupResult]:
            nonlocal cleanup_task
            if cleanup_task is None:
                self._phase = SupervisorPhase.DRAINING
                self._log_supervisor(LogState.DRAINING)
                cleanup_task = asyncio.create_task(
                    self._shutdown_continuation(
                        started,
                        runners,
                        operations,
                    ),
                    name="hermes-connector:supervisor-cleanup-continuation",
                )
                self._track_task(cleanup_task)
            return cleanup_task

        async def shutdown_once() -> _CleanupResult:
            task = cleanup_once()
            deadline = require_shutdown_deadline()
            if task.done():
                return self._apply_cleanup_deadline(
                    self._completed_cleanup_result(task),
                    deadline,
                )

            remaining = max(0.0, deadline - self._now())
            if remaining <= 0:
                return _CleanupResult(failed=False, deadline_exceeded=True)

            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task not in done:
                return _CleanupResult(failed=False, deadline_exceeded=True)
            return self._apply_cleanup_deadline(
                self._completed_cleanup_result(task),
                deadline,
            )

        try:
            start_deadline = self._now() + self._config.start_deadline_seconds
            for component in self._components:
                if self._stop_is_requested():
                    raise _StopDuringStartup
                started.append(component)
                self._set_component_state(component, LogState.STARTING)
                try:
                    await self._await_startup_operation(
                        component.start(),
                        runners=runners,
                        operations=operations,
                        deadline=start_deadline,
                        task_name=f"hermes-connector:{component.name}:start",
                    )
                except (_StartDeadline, _StopDuringStartup):
                    raise
                except BaseException as error:  # noqa: BLE001 - component boundary
                    raise self._component_failure(error) from None
                runner = asyncio.create_task(
                    component.run(),
                    name=f"hermes-connector:{component.name}",
                )
                runners.append(runner)
                self._track_task(runner)

            for component in self._components:
                if self._stop_is_requested():
                    raise _StopDuringStartup
                try:
                    ready = await self._await_startup_operation(
                        component.ready(),
                        runners=runners,
                        operations=operations,
                        deadline=start_deadline,
                        task_name=f"hermes-connector:{component.name}:ready",
                    )
                except (_StartDeadline, _StopDuringStartup, _ComponentFailure):
                    raise
                except BaseException as error:  # noqa: BLE001 - component boundary
                    raise self._component_failure(error) from None
                if not ready:
                    raise _ComponentNotReady(f"component not ready: {component.name}")
                self._set_component_state(component, LogState.READY)

            if self._stop_is_requested():
                raise _StopDuringStartup
            reached_ready = True
            self._phase = SupervisorPhase.READY
            self._log_supervisor(LogState.READY)
            self._resolve_startup()

            await self._wait_for_stop_or_runner(runners)
            cleanup_result = await shutdown_once()
            if cleanup_result.deadline_exceeded:
                raise _CleanupDeadline
            if cleanup_result.failed:
                raise _CleanupFailed
            self._phase = SupervisorPhase.STOPPED
            self._log_supervisor(LogState.STOPPED)
        except _StopDuringStartup:
            cleanup_result = await shutdown_once()
            if cleanup_result.deadline_exceeded or cleanup_result.failed:
                category = (
                    "stop_deadline"
                    if cleanup_result.deadline_exceeded
                    else "component_failure"
                )
                self._mark_failed(category)
                self._reject_startup(f"supervisor startup failed: {category}")
                return
            self._phase = SupervisorPhase.STOPPED
            self._log_supervisor(LogState.STOPPED)
            self._reject_startup("supervisor startup was stopped")
        except asyncio.CancelledError:
            cleanup_result = await shutdown_once()
            category = (
                "stop_deadline" if cleanup_result.deadline_exceeded else "cancelled"
            )
            self._mark_failed(category)
            self._reject_startup("supervisor startup was cancelled")
            raise
        except BaseException as error:  # noqa: BLE001 - lifecycle cleanup boundary
            cleanup_result = await shutdown_once()
            category = self._classify_failure(
                error,
                reached_ready,
                cleanup_result,
            )
            self._mark_failed(category)
            self._reject_startup(
                f"supervisor startup failed: {category}",
                category=category,
                retryable=self._failure_is_retryable(error, category),
            )

    async def _cleanup_components(
        self,
        started: list[ComponentPort],
    ) -> _CleanupResult:
        failed = False

        async def cleanup_step(
            component: ComponentPort,
            state: LogState,
            action,
        ) -> bool:
            nonlocal failed
            try:
                self._set_component_state(component, state)
            except Exception:  # noqa: BLE001 - cleanup must still run
                failed = True

            try:
                await action()
            except TimeoutError:
                failed = True
                return False
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                failed = True
                return False
            except BaseException:  # noqa: BLE001 - safe cleanup category boundary
                failed = True
                return False
            return True

        for component in reversed(started):
            await cleanup_step(
                component,
                LogState.DRAINING,
                component.drain,
            )
        for component in reversed(started):
            stopped = await cleanup_step(
                component,
                LogState.STOPPING,
                component.stop,
            )
            if stopped:
                try:
                    self._set_component_state(component, LogState.STOPPED)
                except Exception:  # noqa: BLE001 - cleanup result remains safe
                    failed = True

        if failed:
            for component in started:
                self._component_states[component.name] = LogState.FAILED
        return _CleanupResult(
            failed=failed,
            deadline_exceeded=False,
        )

    async def _shutdown_continuation(
        self,
        started: list[ComponentPort],
        runners: list[asyncio.Task[None]],
        operations: list[asyncio.Task[object]],
    ) -> _CleanupResult:
        operation_result = await self._join_startup_operations(operations)
        cleanup_result = await self._cleanup_components(started)
        runner_result = await self._terminate_runners(runners)
        return _CleanupResult(
            failed=(
                operation_result.failed or cleanup_result.failed or runner_result.failed
            ),
            deadline_exceeded=False,
        )

    async def _join_startup_operations(
        self,
        operations: list[asyncio.Task[object]],
    ) -> _CleanupResult:
        results = await asyncio.gather(*operations, return_exceptions=True)
        failed = any(
            isinstance(result, BaseException)
            and not isinstance(result, asyncio.CancelledError)
            for result in results
        )
        return _CleanupResult(failed=failed, deadline_exceeded=False)

    async def _await_startup_operation(
        self,
        operation: Awaitable[ResultT],
        *,
        runners: list[asyncio.Task[None]],
        operations: list[asyncio.Task[object]],
        deadline: float,
        task_name: str,
    ) -> ResultT:
        operation_task = asyncio.create_task(operation, name=task_name)
        operations.append(operation_task)
        self._track_task(operation_task)
        stop_task = asyncio.create_task(
            self._require_stop_event().wait(),
            name="hermes-connector:supervisor-stop-wait",
        )
        self._track_task(stop_task)
        cancel_requested = False

        def cancel_operation_once() -> None:
            nonlocal cancel_requested
            if not cancel_requested and not operation_task.done():
                cancel_requested = True
                operation_task.cancel()

        try:
            remaining = max(0.0, deadline - self._now())
            done, _ = await asyncio.wait(
                {operation_task, stop_task, *runners},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                cancel_operation_once()
                raise _StartDeadline
            if stop_task in done and self._stop_is_requested():
                cancel_operation_once()
                raise _StopDuringStartup
            completed_runner = next(
                (runner for runner in runners if runner in done), None
            )
            if completed_runner is not None:
                cancel_operation_once()
                raise self._component_failure(self._task_exception(completed_runner))
            try:
                result = await operation_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                raise _ComponentFailure from None
            except BaseException as error:  # noqa: BLE001 - component boundary
                raise self._component_failure(error) from None
            if self._now() > deadline:
                raise _StartDeadline
            return result
        finally:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                cancel_operation_once()
            if not stop_task.done():
                stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task

    async def _wait_for_stop_or_runner(
        self,
        runners: list[asyncio.Task[None]],
    ) -> None:
        stop_task = asyncio.create_task(
            self._require_stop_event().wait(),
            name="hermes-connector:supervisor-stop-wait",
        )
        self._track_task(stop_task)
        try:
            done, _ = await asyncio.wait(
                {stop_task, *runners},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and self._stop_is_requested():
                return
            completed_runner = next(
                (runner for runner in runners if runner in done), None
            )
            if completed_runner is not None:
                raise self._component_failure(self._task_exception(completed_runner))
        finally:
            if not stop_task.done():
                stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task

    async def _terminate_runners(
        self,
        runners: list[asyncio.Task[None]],
    ) -> _CleanupResult:
        unique_tasks = tuple(dict.fromkeys(runners))
        failed = False
        pending = {task for task in unique_tasks if not task.done()}
        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for task in unique_tasks:
            if not task.done() or task.cancelled():
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                continue
            if error is not None:
                failed = True
        return _CleanupResult(
            failed=failed,
            deadline_exceeded=False,
        )

    @staticmethod
    def _completed_cleanup_result(
        task: asyncio.Task[_CleanupResult],
    ) -> _CleanupResult:
        if task.cancelled():
            return _CleanupResult(failed=True, deadline_exceeded=False)
        try:
            return task.result()
        except BaseException:  # noqa: BLE001 - cleanup result category boundary
            return _CleanupResult(failed=True, deadline_exceeded=False)

    def _apply_cleanup_deadline(
        self,
        result: _CleanupResult,
        deadline: float,
    ) -> _CleanupResult:
        return _CleanupResult(
            failed=result.failed,
            deadline_exceeded=result.deadline_exceeded or self._now() >= deadline,
        )

    def _set_component_state(
        self,
        component: ComponentPort,
        state: LogState,
    ) -> None:
        self._component_states[component.name] = state
        self._safe_emit(
            category=LogCategory.LIFECYCLE,
            component=component.name,
            state=state,
        )

    def _log_supervisor(self, state: LogState) -> None:
        self._safe_emit(
            category=LogCategory.LIFECYCLE,
            component="supervisor",
            state=state,
        )

    def _mark_failed(self, category: str) -> None:
        self._failure_category = category
        self._phase = SupervisorPhase.FAILED
        self._safe_emit(
            category=LogCategory.HEALTH,
            component="supervisor",
            state=LogState.FAILED,
        )

    def _safe_emit(
        self,
        *,
        category: LogCategory,
        component: str,
        state: LogState,
    ) -> None:
        try:
            self._logger.emit(
                category=category,
                component=component,
                state=state,
            )
        except BaseException:  # noqa: BLE001, S110 - lifecycle isolation boundary
            pass

    def _resolve_startup(self) -> None:
        if self._startup_result is not None and not self._startup_result.done():
            self._startup_result.set_result(None)

    def _reject_startup(
        self,
        message: str,
        *,
        category: str = "startup_failure",
        retryable: bool = False,
    ) -> None:
        if self._startup_result is not None and not self._startup_result.done():
            self._startup_result.set_exception(
                SupervisorStartError(
                    message,
                    category=category,
                    retryable=retryable,
                )
            )

    def _track_task(self, task: asyncio.Task[object]) -> None:
        self._owned_tasks.add(task)
        task.add_done_callback(self._observe_task)

    def _observe_task(self, task: asyncio.Task[object]) -> None:
        self._owned_tasks.discard(task)
        if task.cancelled():
            return
        with suppress(BaseException):
            task.exception()

    @staticmethod
    def _observe_future(future: asyncio.Future[None]) -> None:
        if future.cancelled():
            return
        with suppress(BaseException):
            future.exception()

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_running_loop().time()

    def _require_stop_event(self) -> asyncio.Event:
        if self._stop_requested is None:
            raise RuntimeError("stop event was not initialized")
        return self._stop_requested

    def _stop_is_requested(self) -> bool:
        return self._stop_requested is not None and self._stop_requested.is_set()

    @staticmethod
    def _classify_failure(
        error: BaseException,
        reached_ready: bool,
        cleanup_result: _CleanupResult,
    ) -> str:
        if cleanup_result.deadline_exceeded or Supervisor._contains_error(
            error,
            _CleanupDeadline,
        ):
            return "stop_deadline"
        if not reached_ready and Supervisor._contains_error(error, _StartDeadline):
            return "start_deadline"
        if Supervisor._contains_error(error, _ComponentNotReady):
            return "component_not_ready"
        component_failure = Supervisor._find_component_failure(error)
        if component_failure is not None:
            return component_failure.category
        if Supervisor._contains_error(error, _CleanupFailed):
            return "component_failure"
        if isinstance(error, BaseExceptionGroup):
            return "component_failure"
        return "runtime_failure" if reached_ready else "startup_failure"

    @staticmethod
    def _component_failure(error: BaseException | None) -> _ComponentFailure:
        if (
            getattr(error, "error_name", None) == "local_runtime_unavailable"
            and getattr(error, "retryable", None) is True
        ):
            return _ComponentFailure(
                category="local_runtime_unavailable",
                retryable=True,
            )
        if isinstance(error, _ComponentFailure):
            return error
        return _ComponentFailure()

    @staticmethod
    def _task_exception(task: asyncio.Task[None]) -> BaseException | None:
        if task.cancelled():
            return None
        try:
            return task.exception()
        except asyncio.CancelledError:
            return None

    @staticmethod
    def _find_component_failure(error: BaseException) -> _ComponentFailure | None:
        if isinstance(error, _ComponentFailure):
            return error
        if isinstance(error, BaseExceptionGroup):
            for nested in error.exceptions:
                match = Supervisor._find_component_failure(nested)
                if match is not None:
                    return match
        return None

    @staticmethod
    def _failure_is_retryable(error: BaseException, category: str) -> bool:
        component_failure = Supervisor._find_component_failure(error)
        return (
            category == "local_runtime_unavailable"
            and component_failure is not None
            and component_failure.retryable
        )

    @staticmethod
    def _contains_error(
        error: BaseException,
        error_type: type[BaseException],
    ) -> bool:
        if isinstance(error, error_type):
            return True
        if isinstance(error, BaseExceptionGroup):
            return any(
                Supervisor._contains_error(nested, error_type)
                for nested in error.exceptions
            )
        return False
