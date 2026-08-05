from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from hermes_connector.application.service_runner import (
    SERVICE_TRANSITIONS,
    InvalidServiceTransition,
    ServiceRunner,
    ServiceState,
    transition_service,
)
from hermes_connector.application.supervisor import Supervisor
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.runtime import build_service_runner
from hermes_connector.bootstrap.safe_logging import SafeStructuredLogger

EXPECTED_SERVICE_TRANSITIONS = {
    ServiceState.NEW: {ServiceState.LOCKED, ServiceState.FAILED},
    ServiceState.LOCKED: {ServiceState.RUNNING, ServiceState.FAILED},
    ServiceState.RUNNING: {ServiceState.STOPPING, ServiceState.FAILED},
    ServiceState.STOPPING: {ServiceState.STOPPED, ServiceState.FAILED},
    ServiceState.STOPPED: set(),
    ServiceState.FAILED: set(),
}


class RecordingLock:
    def __init__(
        self,
        timeline: list[str],
        error: BaseException | None = None,
        *,
        hold_before_error: bool = False,
    ) -> None:
        self.timeline = timeline
        self.error = error
        self.hold_before_error = hold_before_error
        self.held = False

    def acquire(self) -> None:
        self.timeline.append("lock.acquire")
        if self.error is not None:
            self.held = self.hold_before_error
            raise self.error
        self.held = True

    def close(self) -> None:
        if self.held:
            self.timeline.append("lock.release")
            self.held = False


class RecordingSupervisor:
    def __init__(
        self,
        timeline: list[str],
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.timeline = timeline
        self.start_error = start_error
        self.stop_error = stop_error
        self.started = asyncio.Event()
        self.completed = asyncio.Event()

    async def start(self) -> None:
        self.timeline.append("supervisor.start")
        self.started.set()
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.timeline.append("supervisor.stop")
        try:
            if self.stop_error is not None:
                raise self.stop_error
        finally:
            self.completed.set()

    async def wait(self) -> None:
        await self.completed.wait()


class MinimalComponent:
    name = "bootstrap_component"

    def __init__(self) -> None:
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        return None

    async def ready(self) -> bool:
        return True

    async def run(self) -> None:
        await self.stopped.wait()

    async def drain(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped.set()


class RuntimeComponent:
    def __init__(
        self,
        name: str,
        timeline: list[str],
        *,
        fail_after_ready: bool = False,
        pause_first_drain: bool = False,
    ) -> None:
        self.name = name
        self._timeline = timeline
        self._fail_after_ready = fail_after_ready
        self._pause_first_drain = pause_first_drain
        self._failure_requested = asyncio.Event()
        self._first_drain_entered = asyncio.Event()
        self._first_drain_release = asyncio.Event()
        self._drain_calls = 0
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._timeline.append(f"{self.name}.start")

    async def ready(self) -> bool:
        self._timeline.append(f"{self.name}.ready")
        return True

    async def run(self) -> None:
        if self._fail_after_ready:
            await self._failure_requested.wait()
            raise RuntimeError("must-never-appear")
        await self._stopped.wait()

    async def drain(self) -> None:
        self._drain_calls += 1
        self._timeline.append(f"{self.name}.drain")
        if self._pause_first_drain and self._drain_calls == 1:
            self._first_drain_entered.set()
            await self._first_drain_release.wait()

    async def stop(self) -> None:
        self._timeline.append(f"{self.name}.stop")
        self._stopped.set()

    def fail(self) -> None:
        self._failure_requested.set()

    async def wait_for_first_drain(self) -> None:
        await self._first_drain_entered.wait()

    def release_first_drain(self) -> None:
        self._first_drain_release.set()


def run(coro_factory: Callable[[], object]) -> None:
    asyncio.run(coro_factory())  # type: ignore[arg-type]


class ServiceStateTest(unittest.TestCase):
    def test_all_allowed_state_changes_are_accepted(self) -> None:
        for source, targets in EXPECTED_SERVICE_TRANSITIONS.items():
            for target in targets:
                with self.subTest(source=source.value, target=target.value):
                    self.assertIs(transition_service(source, target), target)

    def test_all_unlisted_state_changes_are_rejected(self) -> None:
        for source in ServiceState:
            for target in ServiceState:
                if target in EXPECTED_SERVICE_TRANSITIONS[source]:
                    continue
                with (
                    self.subTest(
                        source=source.value,
                        target=target.value,
                    ),
                    self.assertRaises(InvalidServiceTransition),
                ):
                    transition_service(source, target)

    def test_service_state_rules_are_complete_and_immutable(self) -> None:
        self.assertEqual(set(SERVICE_TRANSITIONS), set(ServiceState))
        self.assertEqual(
            {source: set(targets) for source, targets in SERVICE_TRANSITIONS.items()},
            EXPECTED_SERVICE_TRANSITIONS,
        )
        with self.assertRaises(TypeError):
            SERVICE_TRANSITIONS[ServiceState.NEW] = frozenset()  # type: ignore[index]


class ServiceRunnerTest(unittest.TestCase):
    def test_start_and_stop_hold_lock_around_supervisor_lifetime(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            runner = ServiceRunner(
                RecordingLock(timeline),
                RecordingSupervisor(timeline),
            )

            await runner.start()
            self.assertIs(runner.state, ServiceState.RUNNING)
            await runner.stop()
            await runner.stop()

            self.assertIs(runner.state, ServiceState.STOPPED)
            self.assertEqual(
                timeline,
                [
                    "lock.acquire",
                    "supervisor.start",
                    "supervisor.stop",
                    "lock.release",
                ],
            )

        run(scenario)

    def test_lock_failure_never_starts_supervisor(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            runner = ServiceRunner(
                RecordingLock(timeline, RuntimeError("lock failed")),
                RecordingSupervisor(timeline),
            )

            with self.assertRaisesRegex(RuntimeError, "lock failed"):
                await runner.start()

            self.assertIs(runner.state, ServiceState.FAILED)
            self.assertEqual(timeline, ["lock.acquire"])

        run(scenario)

    def test_partial_lock_acquire_failure_is_closed(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            runner = ServiceRunner(
                RecordingLock(
                    timeline,
                    RuntimeError("partial lock failure"),
                    hold_before_error=True,
                ),
                RecordingSupervisor(timeline),
            )

            with self.assertRaisesRegex(RuntimeError, "partial lock failure"):
                await runner.start()

            self.assertIs(runner.state, ServiceState.FAILED)
            self.assertEqual(timeline, ["lock.acquire", "lock.release"])

        run(scenario)

    def test_supervisor_start_failure_stops_then_releases_lock(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            runner = ServiceRunner(
                RecordingLock(timeline),
                RecordingSupervisor(
                    timeline,
                    start_error=RuntimeError("supervisor failed"),
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "supervisor failed"):
                await runner.start()

            self.assertIs(runner.state, ServiceState.FAILED)
            self.assertEqual(
                timeline,
                [
                    "lock.acquire",
                    "supervisor.start",
                    "supervisor.stop",
                    "lock.release",
                ],
            )

        run(scenario)

    def test_supervisor_stop_failure_still_releases_lock(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            runner = ServiceRunner(
                RecordingLock(timeline),
                RecordingSupervisor(
                    timeline,
                    stop_error=RuntimeError("stop failed"),
                ),
            )
            await runner.start()

            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                await runner.stop()

            self.assertIs(runner.state, ServiceState.FAILED)
            self.assertEqual(
                timeline,
                [
                    "lock.acquire",
                    "supervisor.start",
                    "supervisor.stop",
                    "lock.release",
                ],
            )

        run(scenario)

    def test_run_until_waits_for_event_then_stops(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            supervisor = RecordingSupervisor(timeline)
            runner = ServiceRunner(RecordingLock(timeline), supervisor)
            stop_event = asyncio.Event()

            service_task = asyncio.create_task(runner.run_until(stop_event))
            await supervisor.started.wait()
            self.assertIs(runner.state, ServiceState.RUNNING)
            stop_event.set()
            await service_task

            self.assertIs(runner.state, ServiceState.STOPPED)
            self.assertEqual(timeline[-2:], ["supervisor.stop", "lock.release"])

        run(scenario)

    def test_run_until_cancellation_also_stops_and_releases(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            supervisor = RecordingSupervisor(timeline)
            runner = ServiceRunner(RecordingLock(timeline), supervisor)

            service_task = asyncio.create_task(runner.run_until(asyncio.Event()))
            await supervisor.started.wait()
            service_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await service_task

            self.assertIs(runner.state, ServiceState.STOPPED)
            self.assertEqual(timeline[-2:], ["supervisor.stop", "lock.release"])

        run(scenario)

    def test_runtime_component_failure_ends_service_and_releases_lock(self) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            components = [
                RuntimeComponent("sqlite_storage", timeline),
                RuntimeComponent("local_gateway", timeline),
                RuntimeComponent(
                    "cloud_wss",
                    timeline,
                    fail_after_ready=True,
                ),
            ]
            lock = RecordingLock(timeline)
            supervisor = Supervisor(
                components,
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            runner = ServiceRunner(lock, supervisor)
            service_task = asyncio.create_task(runner.run_until(asyncio.Event()))

            try:
                while runner.state is not ServiceState.RUNNING:
                    await asyncio.sleep(0)
                components[-1].fail()
                done, _ = await asyncio.wait({service_task}, timeout=0.2)

                self.assertEqual(done, {service_task})
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^supervisor runtime failed: component_failure$",
                ) as raised:
                    await service_task

                self.assertNotIn("must-never-appear", str(raised.exception))
                self.assertIs(runner.state, ServiceState.FAILED)
                self.assertFalse(lock.held)
                cleanup = [
                    item for item in timeline if item.endswith((".drain", ".stop"))
                ]
                self.assertEqual(
                    cleanup,
                    [
                        "cloud_wss.drain",
                        "local_gateway.drain",
                        "sqlite_storage.drain",
                        "cloud_wss.stop",
                        "local_gateway.stop",
                        "sqlite_storage.stop",
                    ],
                )
                timeline_before_second_stop = list(timeline)
                await runner.stop()
                self.assertEqual(timeline, timeline_before_second_stop)
            finally:
                if not service_task.done():
                    service_task.cancel()
                    with suppress(BaseException):
                        await service_task

        run(scenario)

    def test_signal_and_component_failure_share_one_cleanup_and_release_lock(
        self,
    ) -> None:
        async def scenario() -> None:
            timeline: list[str] = []
            component = RuntimeComponent(
                "cloud_wss",
                timeline,
                fail_after_ready=True,
                pause_first_drain=True,
            )
            lock = RecordingLock(timeline)
            supervisor = Supervisor(
                [component],
                ConnectorConfig(
                    start_deadline_seconds=0.1,
                    stop_deadline_seconds=0.1,
                ),
                SafeStructuredLogger(lambda _: None),
            )
            runner = ServiceRunner(lock, supervisor)
            stop_event = asyncio.Event()
            service_task = asyncio.create_task(runner.run_until(stop_event))

            try:
                while runner.state is not ServiceState.RUNNING:
                    await asyncio.sleep(0)
                stop_event.set()
                await asyncio.wait_for(component.wait_for_first_drain(), timeout=0.1)
                component.fail()
                asyncio.get_running_loop().call_later(
                    0.01,
                    component.release_first_drain,
                )

                with self.assertRaises(RuntimeError) as raised:
                    await asyncio.wait_for(service_task, timeout=0.2)

                self.assertNotIn("must-never-appear", str(raised.exception))
                self.assertIs(runner.state, ServiceState.FAILED)
                self.assertFalse(lock.held)
                self.assertEqual(
                    [item for item in timeline if item.endswith((".drain", ".stop"))],
                    ["cloud_wss.drain", "cloud_wss.stop"],
                )
                self.assertEqual(supervisor.owned_task_count, 0)
            finally:
                if not service_task.done():
                    service_task.cancel()
                    with suppress(BaseException):
                        await service_task

        run(scenario)

    def test_bootstrap_builds_a_side_effect_free_runner(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                lock_path = Path(directory) / "connector.lock"
                runner = build_service_runner(
                    lock_path=lock_path,
                    components=[MinimalComponent()],
                    config=ConnectorConfig(),
                    logger=SafeStructuredLogger(lambda _: None),
                    platform_name="darwin",
                )

                self.assertFalse(lock_path.exists())
                await runner.start()
                self.assertTrue(lock_path.exists())
                await runner.stop()
                self.assertTrue(lock_path.exists())

        run(scenario)


if __name__ == "__main__":
    unittest.main()
