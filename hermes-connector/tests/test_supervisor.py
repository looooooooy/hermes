from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.supervisor import (
    Supervisor,
    SupervisorPhase,
    SupervisorRuntimeError,
    SupervisorStartError,
    SupervisorStopError,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.safe_logging import SafeStructuredLogger
from hermes_connector.domain.storage import StorageError
from hermes_connector.ports.logging import LogCategory, LogState


class SelectivelyFailingLogger:
    def __init__(
        self,
        *,
        component: str,
        state: LogState,
    ) -> None:
        self._component = component
        self._state = state

    def emit(
        self,
        *,
        category: LogCategory,
        component: str,
        state: LogState,
    ) -> None:
        del category
        if component == self._component and state is self._state:
            raise RuntimeError("must-never-appear")


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class CancellationCleanupComponent:
    name = "cancellation_cleanup"

    def __init__(self) -> None:
        self.sequence: list[str] = []
        self.start_entered = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release_cancellation_cleanup = asyncio.Event()
        self.release_run = asyncio.Event()
        self.cancel_count = 0

    async def start(self) -> None:
        self.sequence.append("start")
        self.start_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_count += 1
            self.sequence.append("start-cancelled")
            self.cancel_seen.set()
            try:
                await self.release_cancellation_cleanup.wait()
            except asyncio.CancelledError:
                self.cancel_count += 1
                self.sequence.append("start-cancelled-again")
                await self.release_cancellation_cleanup.wait()
            self.sequence.append("start-cleanup-finished")
            raise

    async def ready(self) -> bool:
        return True

    async def run(self) -> None:
        await self.release_run.wait()

    async def drain(self) -> None:
        self.sequence.append("drain")

    async def stop(self) -> None:
        self.sequence.append("stop")
        self.release_run.set()


class BlockingOpenSQLiteStorage(SQLiteStorageComponent):
    def __init__(
        self,
        path: Path,
        config: ConnectorConfig,
        *,
        open_entered: threading.Event,
        release_open: threading.Event,
    ) -> None:
        super().__init__(path, config)
        self.open_entered = open_entered
        self.release_open = release_open
        self.sequence: list[str] = []

    async def start(self) -> None:
        try:
            await super().start()
        finally:
            self.sequence.append("start-finished")

    async def drain(self) -> None:
        self.sequence.append("drain")
        await super().drain()

    async def stop(self) -> None:
        self.sequence.append("stop")
        await super().stop()

    def _open(self) -> None:
        self.open_entered.set()
        self.release_open.wait()
        super()._open()


class RecordingComponent:
    def __init__(
        self,
        name: str,
        *,
        ready_result: bool = True,
        fail_before_ready: bool = False,
        fail_after_ready: bool = False,
        runtime_error: BaseException | None = None,
        fail_on_drain: bool = False,
        fail_on_stop: bool = False,
        timeout_on_start: bool = False,
        timeout_on_ready: bool = False,
        timeout_on_drain: bool = False,
        timeout_on_stop: bool = False,
        hang_on_start: bool = False,
        hang_on_ready: bool = False,
        hang_on_drain: bool = False,
        hang_on_stop: bool = False,
        start_delay: float = 0.0,
        ready_delay: float = 0.0,
        drain_delay: float = 0.0,
        stop_delay: float = 0.0,
        trace: list[str] | None = None,
        clock: ManualClock | None = None,
        return_after_ready: bool = False,
        block_run_cancellation: bool = False,
        release_run_on_stop: bool = True,
    ) -> None:
        self.name = name
        self.calls: list[str] = []
        self.ready_result = ready_result
        self.fail_before_ready = fail_before_ready
        self.fail_after_ready = fail_after_ready
        self.runtime_error = runtime_error
        self.fail_on_drain = fail_on_drain
        self.fail_on_stop = fail_on_stop
        self.timeout_on_start = timeout_on_start
        self.timeout_on_ready = timeout_on_ready
        self.timeout_on_drain = timeout_on_drain
        self.timeout_on_stop = timeout_on_stop
        self.hang_on_start = hang_on_start
        self.hang_on_ready = hang_on_ready
        self.hang_on_drain = hang_on_drain
        self.hang_on_stop = hang_on_stop
        self.start_delay = start_delay
        self.ready_delay = ready_delay
        self.drain_delay = drain_delay
        self.stop_delay = stop_delay
        self.trace = trace
        self.clock = clock
        self.return_after_ready = return_after_ready
        self.block_run_cancellation = block_run_cancellation
        self.release_run_on_stop = release_run_on_stop
        self.start_entered = asyncio.Event()
        self.ready_entered = asyncio.Event()
        self.drain_entered = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.failure_requested = asyncio.Event()
        self.release_run = asyncio.Event()
        self.never = asyncio.Event()

    def _record(self, action: str) -> None:
        self.calls.append(action)
        if self.trace is not None:
            self.trace.append(f"{self.name}:{action}")

    async def start(self) -> None:
        self._record("start")
        self.start_entered.set()
        if self.timeout_on_start:
            raise TimeoutError("must-never-appear")
        if self.hang_on_start:
            await self.never.wait()
        await self._delay(self.start_delay)

    async def ready(self) -> bool:
        self._record("ready")
        self.ready_entered.set()
        if self.timeout_on_ready:
            raise TimeoutError("must-never-appear")
        if self.hang_on_ready:
            await self.never.wait()
        if self.fail_before_ready:
            await self.never.wait()
        await self._delay(self.ready_delay)
        return self.ready_result

    async def run(self) -> None:
        if self.fail_before_ready:
            await self.ready_entered.wait()
            raise self.runtime_error or RuntimeError("component child failed")
        if self.fail_after_ready:
            await self.failure_requested.wait()
            raise self.runtime_error or RuntimeError("must-never-appear")
        if self.return_after_ready:
            await self.failure_requested.wait()
            return
        try:
            await self.release_run.wait()
        except asyncio.CancelledError:
            if not self.block_run_cancellation:
                raise
            await self.never.wait()

    async def drain(self) -> None:
        self._record("drain")
        self.drain_entered.set()
        if self.timeout_on_drain:
            raise TimeoutError("must-never-appear")
        if self.hang_on_drain:
            await self.never.wait()
        if self.fail_on_drain:
            raise RuntimeError("must-never-appear")
        await self._delay(self.drain_delay)

    async def stop(self) -> None:
        self._record("stop")
        self.stop_entered.set()
        if self.timeout_on_stop:
            raise TimeoutError("must-never-appear")
        if self.fail_on_stop:
            raise RuntimeError("must-never-appear")
        if self.hang_on_stop:
            await self.never.wait()
        await self._delay(self.stop_delay)
        if self.release_run_on_stop:
            self.release_run.set()

    def fail(self) -> None:
        self.failure_requested.set()

    async def _delay(self, seconds: float) -> None:
        if not seconds:
            return
        if self.clock is None:
            await asyncio.sleep(seconds)
            return
        self.clock.advance(seconds)
        await asyncio.sleep(0)


def run(coro_factory: Callable[[], object]) -> None:
    asyncio.run(coro_factory())  # type: ignore[arg-type]


class SupervisorTest(unittest.TestCase):
    def test_concurrent_start_calls_claim_exactly_one_lifecycle(self) -> None:
        async def scenario() -> None:
            for _ in range(50):
                component = RecordingComponent("local_gateway", hang_on_start=True)
                supervisor = Supervisor(
                    [component],
                    ConnectorConfig(
                        start_deadline_seconds=0.5,
                        stop_deadline_seconds=0.5,
                    ),
                    SafeStructuredLogger(lambda _: None),
                )

                first = asyncio.create_task(supervisor.start())
                second = asyncio.create_task(supervisor.start())
                await component.start_entered.wait()
                component.never.set()
                results = await asyncio.wait_for(
                    asyncio.gather(first, second, return_exceptions=True),
                    timeout=0.5,
                )
                await supervisor.stop()

                self.assertEqual(results.count(None), 1)
                errors = [
                    result
                    for result in results
                    if isinstance(result, SupervisorStartError)
                ]
                self.assertEqual(len(errors), 1)
                self.assertEqual(
                    str(errors[0]),
                    "supervisor can only be started once",
                )
                self.assertEqual(component.calls.count("start"), 1)
                self.assertEqual(component.calls.count("drain"), 1)
                self.assertEqual(component.calls.count("stop"), 1)

        run(scenario)

    def test_start_stop_race_never_reaches_ready_after_stop_returns(self) -> None:
        async def scenario() -> None:
            for _ in range(25):
                component = RecordingComponent("local_gateway", hang_on_start=True)
                supervisor = Supervisor(
                    [component],
                    ConnectorConfig(
                        start_deadline_seconds=0.5,
                        stop_deadline_seconds=0.1,
                    ),
                    SafeStructuredLogger(lambda _: None),
                )

                starter = asyncio.create_task(supervisor.start())
                await component.start_entered.wait()
                stopper = asyncio.create_task(supervisor.stop())
                with self.assertRaisesRegex(
                    SupervisorStartError,
                    "^supervisor startup was stopped$",
                ):
                    await starter
                await stopper

                self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STOPPED)
                await asyncio.sleep(0)
                self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STOPPED)
                self.assertLessEqual(component.calls.count("drain"), 1)
                self.assertLessEqual(component.calls.count("stop"), 1)

        run(scenario)

    def test_component_normal_return_before_stop_is_component_failure(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                return_after_ready=True,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            component.failure_requested.set()
            with self.assertRaisesRegex(
                SupervisorRuntimeError,
                "^supervisor runtime failed: component_failure$",
            ):
                await asyncio.wait_for(supervisor.wait(), timeout=0.5)

            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_normal_return_during_startup_fails_without_hanging(
        self,
    ) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                return_after_ready=True,
            )
            component.failure_requested.set()
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_failure$",
            ):
                await asyncio.wait_for(supervisor.start(), timeout=0.5)

            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_cancelled_start_waiter_does_not_cancel_shared_lifecycle(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_start=True)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SafeStructuredLogger(lambda _: None),
            )

            starter = asyncio.create_task(supervisor.start())
            await component.start_entered.wait()
            starter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await starter
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STARTING)

            component.never.set()
            await asyncio.wait_for(component.ready_entered.wait(), timeout=0.5)
            for _ in range(10):
                if supervisor.snapshot().phase is SupervisorPhase.READY:
                    break
                await asyncio.sleep(0)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.READY)
            await supervisor.stop()

        run(scenario)

    def test_cancelled_stop_waiter_does_not_cancel_shared_shutdown(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_drain=True)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(stop_deadline_seconds=1.0),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            cancelled_waiter = asyncio.create_task(supervisor.stop())
            await component.drain_entered.wait()
            surviving_waiter = asyncio.create_task(supervisor.stop())
            cancelled_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_waiter
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.DRAINING)

            component.never.set()
            await asyncio.wait_for(surviving_waiter, timeout=0.5)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STOPPED)
            self.assertEqual(component.calls.count("drain"), 1)
            self.assertEqual(component.calls.count("stop"), 1)

        run(scenario)

    def test_cancelled_wait_waiter_does_not_change_other_waiter_result(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                fail_after_ready=True,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            cancelled_waiter = asyncio.create_task(supervisor.wait())
            surviving_waiter = asyncio.create_task(supervisor.wait())
            await asyncio.sleep(0)
            cancelled_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_waiter
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.READY)

            component.fail()
            with self.assertRaisesRegex(
                SupervisorRuntimeError,
                "^supervisor runtime failed: component_failure$",
            ):
                await asyncio.wait_for(surviving_waiter, timeout=0.5)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)

        run(scenario)

    def test_stop_deadline_includes_runner_termination_join(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                block_run_cancellation=True,
                release_run_on_stop=False,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(stop_deadline_seconds=0.02),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            stopper = asyncio.create_task(supervisor.stop())
            done, _ = await asyncio.wait({stopper}, timeout=0.5)
            try:
                self.assertIn(stopper, done)
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await stopper
            finally:
                component.never.set()
                if not stopper.done():
                    with suppress(SupervisorStopError):
                        await stopper

            for _ in range(10):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_drain_deadline_keeps_one_tracked_reverse_stop_continuation(
        self,
    ) -> None:
        async def scenario() -> None:
            trace: list[str] = []
            components = [
                RecordingComponent("first", trace=trace),
                RecordingComponent(
                    "second",
                    hang_on_drain=True,
                    trace=trace,
                ),
            ]
            supervisor = Supervisor(
                components,
                ConnectorConfig(stop_deadline_seconds=0.02),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            try:
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await asyncio.wait_for(supervisor.stop(), timeout=0.5)

                self.assertEqual(
                    trace[-1],
                    "second:drain",
                )
                self.assertEqual(
                    [component.calls.count("stop") for component in components],
                    [0, 0],
                )
                self.assertGreater(supervisor.owned_task_count, 0)
                self.assertTrue(
                    any(
                        task.get_name()
                        == "hermes-connector:supervisor-cleanup-continuation"
                        for task in supervisor._owned_tasks
                    )
                )
            finally:
                components[1].never.set()

            for _ in range(100):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(
                trace[-4:],
                [
                    "second:drain",
                    "first:drain",
                    "second:stop",
                    "first:stop",
                ],
            )
            self.assertEqual(
                [component.calls.count("stop") for component in components],
                [1, 1],
            )
            self.assertEqual(supervisor.owned_task_count, 0)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)
            await asyncio.sleep(0)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)

        run(scenario)

    def test_start_cancellation_cleanup_precedes_delayed_drain_and_stop(
        self,
    ) -> None:
        async def scenario() -> None:
            component = CancellationCleanupComponent()
            supervisor = Supervisor(
                [component],
                ConnectorConfig(stop_deadline_seconds=0.02),
                SafeStructuredLogger(lambda _: None),
            )
            starter = asyncio.create_task(supervisor.start())
            await component.start_entered.wait()

            stopper = asyncio.create_task(supervisor.stop())
            await component.cancel_seen.wait()
            try:
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await asyncio.wait_for(stopper, timeout=0.5)
                with self.assertRaisesRegex(
                    SupervisorStartError,
                    "^supervisor startup failed: stop_deadline$",
                ):
                    await starter

                self.assertEqual(
                    component.sequence,
                    ["start", "start-cancelled"],
                )
                self.assertEqual(component.cancel_count, 1)
                self.assertGreater(supervisor.owned_task_count, 0)
            finally:
                component.release_cancellation_cleanup.set()

            for _ in range(100):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(
                component.sequence,
                [
                    "start",
                    "start-cancelled",
                    "start-cleanup-finished",
                    "drain",
                    "stop",
                ],
            )
            self.assertEqual(component.cancel_count, 1)
            self.assertEqual(supervisor.owned_task_count, 0)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)

        run(scenario)

    def test_supervisor_named_tasks_match_owned_tasks_while_ready(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway")
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()
            for _ in range(10):
                live = {
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                    and task.get_name().startswith("hermes-connector:")
                    and not task.done()
                }
                if any(
                    task.get_name() == "hermes-connector:supervisor-stop-wait"
                    for task in live
                ):
                    break
                await asyncio.sleep(0)

            owned = {task for task in supervisor._owned_tasks if not task.done()}
            self.assertEqual(live, owned)
            self.assertTrue(
                any(
                    task.get_name() == "hermes-connector:supervisor-stop-wait"
                    for task in owned
                )
            )

            await supervisor.stop()
            self.assertEqual(supervisor.owned_task_count, 0)
            self.assertFalse(
                any(
                    task.get_name().startswith("hermes-connector:")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )

        run(scenario)

    def test_real_sqlite_blocked_worker_returns_stop_deadline_then_disposes(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            entered = threading.Event()
            release = threading.Event()

            def block_writer(_: str) -> None:
                entered.set()
                release.wait()

            storage = SQLiteStorageComponent(
                path,
                ConnectorConfig(
                    stop_deadline_seconds=0.03,
                    storage_write_deadline_seconds=1.0,
                ),
                write_fault=block_writer,
            )
            supervisor = Supervisor(
                [storage],
                ConnectorConfig(
                    stop_deadline_seconds=0.03,
                    storage_write_deadline_seconds=1.0,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()
            write = asyncio.create_task(
                storage.put_inbox(
                    message_id="blocked-shutdown",
                    digest="f" * 64,
                    payload=b"{}",
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 0.5))

            stopper = asyncio.create_task(supervisor.stop())
            done, _ = await asyncio.wait({stopper}, timeout=0.5)
            try:
                self.assertIn(stopper, done)
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await stopper
            finally:
                release.set()
                with suppress(StorageError, asyncio.CancelledError):
                    await write
                if not stopper.done():
                    with suppress(SupervisorStopError):
                        await stopper

            for _ in range(100):
                if storage._executor is None and storage._engine is None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNone(storage._executor)
            self.assertIsNone(storage._engine)
            self.assertEqual(supervisor.owned_task_count, 0)

        with tempfile.TemporaryDirectory() as directory:
            run(lambda: scenario(Path(directory) / "connector.sqlite3"))

    def test_real_sqlite_blocked_open_finishes_before_delayed_cleanup(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            open_entered = threading.Event()
            release_open = threading.Event()
            config = ConnectorConfig(
                start_deadline_seconds=1.0,
                stop_deadline_seconds=0.03,
            )
            storage = BlockingOpenSQLiteStorage(
                path,
                config,
                open_entered=open_entered,
                release_open=release_open,
            )
            supervisor = Supervisor(
                [storage],
                config,
                SafeStructuredLogger(lambda _: None),
            )
            starter = asyncio.create_task(supervisor.start())
            self.assertTrue(await asyncio.to_thread(open_entered.wait, 0.5))

            stopper = asyncio.create_task(supervisor.stop())
            try:
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await asyncio.wait_for(stopper, timeout=0.5)
                with self.assertRaisesRegex(
                    SupervisorStartError,
                    "^supervisor startup failed: stop_deadline$",
                ):
                    await starter

                self.assertEqual(storage.sequence, [])
                self.assertGreater(supervisor.owned_task_count, 0)
                self.assertTrue(
                    any(
                        task.get_name()
                        == "hermes-connector:supervisor-cleanup-continuation"
                        for task in supervisor._owned_tasks
                    )
                )
            finally:
                release_open.set()

            for _ in range(200):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(
                storage.sequence,
                ["start-finished", "drain", "stop"],
            )
            self.assertIsNone(storage._executor)
            self.assertIsNone(storage._engine)
            self.assertEqual(supervisor.owned_task_count, 0)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)
            self.assertFalse(
                any(
                    task.get_name().startswith("hermes-connector:")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            run(lambda: scenario(Path(directory) / "connector.sqlite3"))

    def test_components_follow_start_ready_drain_stop_and_snapshots_are_passive(
        self,
    ) -> None:
        async def scenario() -> None:
            components = [
                RecordingComponent("local_gateway"),
                RecordingComponent("cloud_gateway"),
            ]
            logger = SafeStructuredLogger(lambda _: None)
            supervisor = Supervisor(components, ConnectorConfig(), logger)

            await supervisor.start()
            calls_before_snapshots = [list(component.calls) for component in components]

            first = supervisor.snapshot()
            second = supervisor.snapshot()

            self.assertEqual(first, second)
            self.assertTrue(first.live)
            self.assertTrue(first.ready)
            self.assertIs(first.phase, SupervisorPhase.READY)
            self.assertEqual(
                [list(component.calls) for component in components],
                calls_before_snapshots,
            )

            await supervisor.stop()

            stopped = supervisor.snapshot()
            self.assertFalse(stopped.live)
            self.assertFalse(stopped.ready)
            self.assertIs(stopped.phase, SupervisorPhase.STOPPED)
            for component in components:
                self.assertEqual(
                    component.calls,
                    ["start", "ready", "drain", "stop"],
                )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_not_ready_component_fails_start_and_cleans_up(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", ready_result=False)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(start_deadline_seconds=0.1, stop_deadline_seconds=0.1),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_not_ready$",
            ):
                await supervisor.start()

            snapshot = supervisor.snapshot()
            self.assertFalse(snapshot.live)
            self.assertFalse(snapshot.ready)
            self.assertIs(snapshot.phase, SupervisorPhase.FAILED)
            self.assertEqual(component.calls, ["start", "ready", "drain", "stop"])
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_child_failure_prevents_readiness_and_leaves_no_tasks(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("cloud_gateway", fail_before_ready=True)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(start_deadline_seconds=0.1, stop_deadline_seconds=0.1),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_failure$",
            ):
                await supervisor.start()

            self.assertFalse(supervisor.snapshot().ready)
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)
            self.assertEqual(component.calls, ["start", "ready", "drain", "stop"])
            self.assertEqual(supervisor.owned_task_count, 0)
            self.assertFalse(
                any(
                    task.get_name().startswith("hermes-connector:")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )

        run(scenario)

    def test_local_runtime_unavailable_is_preserved_at_startup_boundary(self) -> None:
        class LocalRuntimeUnavailable(RuntimeError):
            error_name = "local_runtime_unavailable"
            retryable = True

        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                fail_before_ready=True,
                runtime_error=LocalRuntimeUnavailable("must-never-appear"),
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(start_deadline_seconds=0.1, stop_deadline_seconds=0.1),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaises(SupervisorStartError) as raised:
                await supervisor.start()

            self.assertEqual(raised.exception.category, "local_runtime_unavailable")
            self.assertTrue(raised.exception.retryable)
            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(
                supervisor.snapshot().failure_category,
                "local_runtime_unavailable",
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_start_deadline_cancels_startup_without_leaking_tasks(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_start=True)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.01, stop_deadline_seconds=0.05
                ),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: start_deadline$",
            ):
                await supervisor.start()

            self.assertEqual(component.calls, ["start", "drain", "stop"])
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_stop_deadline_cancels_component_runner_without_leaking_tasks(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_stop=True)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(start_deadline_seconds=0.1, stop_deadline_seconds=0.01),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            try:
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await supervisor.stop()

                self.assertFalse(supervisor.snapshot().ready)
                self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)
                self.assertGreater(supervisor.owned_task_count, 0)
            finally:
                component.never.set()

            for _ in range(100):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_runtime_failure_continues_reverse_cleanup_after_component_errors(
        self,
    ) -> None:
        async def scenario() -> None:
            components = [
                RecordingComponent("sqlite_storage"),
                RecordingComponent(
                    "local_gateway",
                    fail_on_drain=True,
                    fail_on_stop=True,
                ),
                RecordingComponent("cloud_wss", fail_after_ready=True),
            ]
            supervisor = Supervisor(
                components,
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            components[-1].fail()
            with self.assertRaisesRegex(
                SupervisorRuntimeError,
                "^supervisor runtime failed: component_failure$",
            ) as raised:
                await supervisor.wait()

            self.assertNotIn("must-never-appear", str(raised.exception))
            for component in components:
                self.assertEqual(
                    component.calls,
                    ["start", "ready", "drain", "stop"],
                )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_stop_deadline_is_global_and_cleanup_is_not_repeated(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_stop=True)
            stop_deadline = 0.04
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=stop_deadline,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            started_at = asyncio.get_running_loop().time()
            try:
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await supervisor.stop()
                elapsed = asyncio.get_running_loop().time() - started_at

                self.assertLess(elapsed, stop_deadline * 1.75)
                self.assertEqual(
                    component.calls,
                    ["start", "ready", "drain", "stop"],
                )
                self.assertGreater(supervisor.owned_task_count, 0)
            finally:
                component.never.set()

            for _ in range(100):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_timeout_after_ready_is_not_a_stop_deadline(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "cloud_wss",
                fail_after_ready=True,
                runtime_error=TimeoutError("must-never-appear"),
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            component.fail()
            with self.assertRaisesRegex(
                SupervisorRuntimeError,
                "^supervisor runtime failed: component_failure$",
            ) as raised:
                await supervisor.wait()

            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_timeout_from_start_is_a_component_failure(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                timeout_on_start=True,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=1.0,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_failure$",
            ) as raised:
                await supervisor.start()

            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(component.calls, ["start", "drain", "stop"])
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_timeout_from_ready_is_a_component_failure(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                timeout_on_ready=True,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=1.0,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_failure$",
            ) as raised:
                await supervisor.start()

            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(component.calls, ["start", "ready", "drain", "stop"])
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_startup_failure_drains_then_stops_started_components_in_reverse(
        self,
    ) -> None:
        async def scenario() -> None:
            trace: list[str] = []
            components = [
                RecordingComponent("local_gateway", trace=trace),
                RecordingComponent(
                    "cloud_gateway",
                    ready_result=False,
                    trace=trace,
                ),
            ]
            supervisor = Supervisor(
                components,
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_not_ready$",
            ):
                await supervisor.start()

            self.assertEqual(
                trace[-4:],
                [
                    "cloud_gateway:drain",
                    "local_gateway:drain",
                    "cloud_gateway:stop",
                    "local_gateway:stop",
                ],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_supervisor_starting_log_failure_does_not_block_startup(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway")
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SelectivelyFailingLogger(
                    component="supervisor",
                    state=LogState.STARTING,
                ),
            )

            await asyncio.wait_for(supervisor.start(), timeout=0.2)
            await supervisor.stop()

            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STOPPED)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_supervisor_draining_log_failure_does_not_interrupt_cleanup(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway")
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SelectivelyFailingLogger(
                    component="supervisor",
                    state=LogState.DRAINING,
                ),
            )

            await supervisor.start()
            await supervisor.stop()

            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STOPPED)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_supervisor_failed_log_failure_does_not_hide_startup_failure(
        self,
    ) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", ready_result=False)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SelectivelyFailingLogger(
                    component="supervisor",
                    state=LogState.FAILED,
                ),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: component_not_ready$",
            ) as raised:
                await asyncio.wait_for(supervisor.start(), timeout=0.2)

            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.FAILED)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_cleanup_log_failures_do_not_change_cleanup_result(self) -> None:
        async def scenario(state: LogState) -> None:
            component = RecordingComponent("local_gateway")
            supervisor = Supervisor(
                [component],
                ConnectorConfig(),
                SelectivelyFailingLogger(
                    component=component.name,
                    state=state,
                ),
            )

            await supervisor.start()
            await supervisor.stop()

            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertIs(supervisor.snapshot().phase, SupervisorPhase.STOPPED)
            self.assertEqual(supervisor.owned_task_count, 0)

        for state in (LogState.DRAINING, LogState.STOPPING, LogState.STOPPED):
            with self.subTest(state=state):
                run(lambda state=state: scenario(state))

    def test_ready_wait_obeys_real_start_deadline(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_ready=True)
            deadline = 0.03
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=deadline,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: start_deadline$",
            ):
                await asyncio.wait_for(supervisor.start(), timeout=0.5)

            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_start_and_ready_share_one_absolute_deadline(self) -> None:
        async def scenario() -> None:
            clock = ManualClock()
            deadline = 0.05
            component = RecordingComponent(
                "local_gateway",
                start_delay=0.035,
                ready_delay=0.035,
                clock=clock,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=deadline,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
                clock=clock,
            )

            with self.assertRaisesRegex(
                SupervisorStartError,
                "^supervisor startup failed: start_deadline$",
            ):
                await asyncio.wait_for(supervisor.start(), timeout=0.5)

            self.assertGreaterEqual(clock(), deadline)
            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_drain_wait_obeys_real_stop_deadline(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent("local_gateway", hang_on_drain=True)
            deadline = 0.03
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=deadline,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            try:
                with self.assertRaisesRegex(
                    SupervisorStopError,
                    "^supervisor stop failed: stop_deadline$",
                ):
                    await asyncio.wait_for(supervisor.stop(), timeout=0.5)

                self.assertEqual(component.calls[:3], ["start", "ready", "drain"])
                self.assertGreater(supervisor.owned_task_count, 0)
            finally:
                component.never.set()

            for _ in range(100):
                if supervisor.owned_task_count == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(component.calls, ["start", "ready", "drain", "stop"])
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_all_components_drain_and_stop_share_one_absolute_deadline(
        self,
    ) -> None:
        async def scenario() -> None:
            clock = ManualClock()
            deadline = 0.05
            components = [
                RecordingComponent(
                    "local_gateway",
                    drain_delay=0.02,
                    stop_delay=0.02,
                    clock=clock,
                ),
                RecordingComponent(
                    "cloud_gateway",
                    drain_delay=0.02,
                    stop_delay=0.02,
                    clock=clock,
                ),
            ]
            supervisor = Supervisor(
                components,
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=deadline,
                ),
                SafeStructuredLogger(lambda _: None),
                clock=clock,
            )
            await supervisor.start()

            with self.assertRaisesRegex(
                SupervisorStopError,
                "^supervisor stop failed: stop_deadline$",
            ):
                await asyncio.wait_for(supervisor.stop(), timeout=0.5)

            self.assertGreaterEqual(clock(), deadline)
            for component in components:
                self.assertIn("drain", component.calls)
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_timeout_from_drain_is_a_component_failure(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                timeout_on_drain=True,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=1.0,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            with self.assertRaisesRegex(
                SupervisorStopError,
                "^supervisor stop failed: component_failure$",
            ) as raised:
                await supervisor.stop()

            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)

    def test_component_timeout_from_stop_is_a_component_failure(self) -> None:
        async def scenario() -> None:
            component = RecordingComponent(
                "local_gateway",
                timeout_on_stop=True,
            )
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=1.0,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()

            with self.assertRaisesRegex(
                SupervisorStopError,
                "^supervisor stop failed: component_failure$",
            ) as raised:
                await supervisor.stop()

            self.assertNotIn("must-never-appear", str(raised.exception))
            self.assertEqual(
                component.calls,
                ["start", "ready", "drain", "stop"],
            )
            self.assertEqual(supervisor.owned_task_count, 0)

        run(scenario)


if __name__ == "__main__":
    unittest.main()
