from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal

import pytest

from hermes_cloud.application import runtime as runtime_module
from hermes_cloud.application.runtime import ComponentRuntime
from hermes_cloud.entrypoints.business_api import create_app as create_business_api
from hermes_cloud.entrypoints.connector_gateway import (
    create_app as create_connector_gateway,
)
from hermes_cloud.entrypoints.file_gateway import create_app as create_file_gateway
from hermes_cloud.entrypoints.worker import create_worker


class Probe:
    def __init__(
        self,
        name: str,
        *,
        critical: bool,
        deadline_seconds: float,
        behavior: Literal["success", "failure", "block"],
    ) -> None:
        self.name = name
        self.critical = critical
        self.deadline_seconds = deadline_seconds
        self.behavior = behavior
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active_checks = 0
        self.maximum_active_checks = 0

    async def check(self) -> None:
        self.calls += 1
        self.active_checks += 1
        self.maximum_active_checks = max(
            self.maximum_active_checks,
            self.active_checks,
        )
        self.started.set()
        try:
            if self.behavior == "failure":
                raise RuntimeError("unit-test-dependency-secret")
            if self.behavior == "block":
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active_checks -= 1


async def _wait_until(
    condition: Callable[[], bool],
    *,
    timeout_seconds: float = 0.5,
) -> None:
    async with asyncio.timeout(timeout_seconds):
        while not condition():
            await asyncio.sleep(0.001)


async def _asgi_get_status(app: Any, path: str) -> int:
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": path,
        },
        receive,
        send,
    )
    return outgoing[0]["status"]


async def _connector_websocket_outgoing(app: Any) -> list[dict[str, Any]]:
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "path": "/api/ws",
            "headers": (),
            "subprotocols": ["hermes.connector.v1"],
        },
        receive,
        send,
    )
    return outgoing


def test_critical_probe_success_allows_readiness() -> None:
    async def scenario() -> None:
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[
                Probe(
                    "postgresql",
                    critical=True,
                    deadline_seconds=0.1,
                    behavior="success",
                )
            ],
        )

        await runtime.startup()

        assert runtime.snapshot() == {
            "component": "business-api",
            "dependencies": [
                {
                    "criticality": "CRITICAL",
                    "error": None,
                    "name": "postgresql",
                    "status": "HEALTHY",
                }
            ],
            "diagnostic": "HEALTHY",
            "error": None,
            "live": True,
            "ready": True,
            "state": "READY",
        }
        await runtime.shutdown()

    asyncio.run(scenario())


def test_critical_probe_failure_keeps_liveness_but_blocks_readiness() -> None:
    async def scenario() -> None:
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[
                Probe(
                    "postgresql",
                    critical=True,
                    deadline_seconds=0.1,
                    behavior="failure",
                )
            ],
        )

        await runtime.startup()
        snapshot = runtime.snapshot()

        assert snapshot["live"] is True
        assert snapshot["ready"] is False
        assert snapshot["state"] == "READY"
        assert snapshot["diagnostic"] == "BLOCKED"
        assert snapshot["dependencies"] == [
            {
                "criticality": "CRITICAL",
                "error": {
                    "category": "DEPENDENCY",
                    "code": "DEPENDENCY_UNAVAILABLE",
                    "retryable": True,
                },
                "name": "postgresql",
                "status": "FAILED",
            }
        ]
        assert "unit-test-dependency-secret" not in repr(snapshot)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_critical_probe_timeout_is_safe_and_leaves_no_probe_task() -> None:
    async def scenario() -> None:
        probe = Probe(
            "postgresql",
            critical=True,
            deadline_seconds=0.01,
            behavior="block",
        )
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[probe],
        )

        await runtime.startup()
        snapshot = runtime.snapshot()

        assert snapshot["live"] is True
        assert snapshot["ready"] is False
        assert snapshot["state"] == "READY"
        assert snapshot["dependencies"] == [
            {
                "criticality": "CRITICAL",
                "error": {
                    "category": "DEPENDENCY",
                    "code": "DEPENDENCY_TIMEOUT",
                    "retryable": True,
                },
                "name": "postgresql",
                "status": "TIMED_OUT",
            }
        ]
        assert probe.cancelled.is_set()
        await runtime.shutdown()

    asyncio.run(scenario())


def test_startup_critical_timeout_recovers_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        probe = Probe(
            "sqlite",
            critical=True,
            deadline_seconds=0.01,
            behavior="block",
        )
        runtime = ComponentRuntime("business-api", dependency_probes=[probe])

        await runtime.startup()
        try:
            blocked = runtime.snapshot()
            assert blocked["state"] == "READY"
            assert blocked["ready"] is False
            assert blocked["diagnostic"] == "BLOCKED"
            assert blocked["dependencies"][0]["status"] == "TIMED_OUT"

            probe.behavior = "success"
            await _wait_until(lambda: runtime.snapshot()["ready"] is True)

            recovered = runtime.snapshot()
            assert probe.calls >= 2
            assert recovered["state"] == "READY"
            assert recovered["diagnostic"] == "HEALTHY"
            assert recovered["error"] is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_runtime_critical_timeout_returns_503_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TenantResolver:
        def tenant_for_subject(self, _subject: str) -> None:
            return None

    class SecretResolver:
        def resolve(self, _reference: str) -> bytes:
            return b"x" * 32

    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        probe = Probe(
            "sqlite",
            critical=True,
            deadline_seconds=0.01,
            behavior="success",
        )
        app = create_business_api(
            dependency_probes=[probe],
            identity_repository=object(),
            tenant_resolver=TenantResolver(),
            secret_resolver=SecretResolver(),
            settings={
                "signing_secret_ref": "unit-test",
                "access_ttl_seconds": 300,
                "refresh_ttl_seconds": 3600,
                "ticket_ttl_seconds": 60,
            },
        )

        await app.startup()
        try:
            probe.behavior = "block"
            await _wait_until(
                lambda: app.snapshot()["dependencies"][0]["status"] == "TIMED_OUT"
            )
            blocked = app.snapshot()
            blocked_response = await app._ready()

            probe.behavior = "success"
            await _wait_until(lambda: app.snapshot()["ready"] is True)
            recovered_response = await app._ready()

            assert blocked["state"] == "READY"
            assert blocked["ready"] is False
            assert blocked["diagnostic"] == "BLOCKED"
            assert blocked["error"] == {
                "category": "DEPENDENCY",
                "code": "DEPENDENCY_TIMEOUT",
                "retryable": True,
            }
            assert blocked_response.status_code == 503
            assert probe.calls >= 3
            assert app.snapshot()["diagnostic"] == "HEALTHY"
            assert recovered_response.status_code == 200
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_refresh_publishes_critical_failure_before_later_probe_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        first = Probe(
            "first-critical",
            critical=True,
            deadline_seconds=0.01,
            behavior="success",
        )
        second = Probe(
            "second-critical",
            critical=True,
            deadline_seconds=1.0,
            behavior="success",
        )
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[first, second],
        )

        await runtime.startup()
        try:
            first.behavior = "failure"
            second.behavior = "block"
            await _wait_until(lambda: second.active_checks == 1)
            snapshot = runtime.snapshot()

            assert snapshot["ready"] is False
            assert snapshot["diagnostic"] == "BLOCKED"
            assert [result["status"] for result in snapshot["dependencies"]] == [
                "FAILED",
                "HEALTHY",
            ]
            assert snapshot["error"] == {
                "category": "DEPENDENCY",
                "code": "DEPENDENCY_UNAVAILABLE",
                "retryable": True,
            }
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_recovery_waits_for_every_critical_probe_in_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        first = Probe(
            "first-critical",
            critical=True,
            deadline_seconds=0.01,
            behavior="failure",
        )
        second = Probe(
            "second-critical",
            critical=True,
            deadline_seconds=1.0,
            behavior="success",
        )
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[first, second],
        )

        await runtime.startup()
        try:
            first.behavior = "success"
            second.behavior = "block"
            await _wait_until(lambda: second.active_checks == 1)
            recovering = runtime.snapshot()

            assert [result["status"] for result in recovering["dependencies"]] == [
                "HEALTHY",
                "HEALTHY",
            ]
            assert recovering["ready"] is False
            assert recovering["diagnostic"] == "BLOCKED"
            assert recovering["error"] is not None

            second.release.set()
            await _wait_until(lambda: runtime.snapshot()["ready"] is True)
            recovered = runtime.snapshot()
            assert recovered["diagnostic"] == "HEALTHY"
            assert recovered["error"] is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_business_ready_returns_503_while_later_probe_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TenantResolver:
        def tenant_for_subject(self, _subject: str) -> None:
            return None

    class SecretResolver:
        def resolve(self, _reference: str) -> bytes:
            return b"x" * 32

    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        first = Probe(
            "first-critical",
            critical=True,
            deadline_seconds=0.01,
            behavior="success",
        )
        second = Probe(
            "second-critical",
            critical=True,
            deadline_seconds=1.0,
            behavior="success",
        )
        app = create_business_api(
            dependency_probes=[first, second],
            identity_repository=object(),
            tenant_resolver=TenantResolver(),
            secret_resolver=SecretResolver(),
            settings={
                "signing_secret_ref": "unit-test",
                "access_ttl_seconds": 300,
                "refresh_ttl_seconds": 3600,
                "ticket_ttl_seconds": 60,
            },
        )

        await app.startup()
        try:
            first.behavior = "failure"
            second.behavior = "block"
            await _wait_until(lambda: second.active_checks == 1)

            response = await app._ready()
            assert response.status_code == 503
            assert app.snapshot()["diagnostic"] == "BLOCKED"
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_connector_ready_and_websocket_fail_closed_during_partial_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Authenticator:
        async def authenticate(self, _bearer_token: str) -> object:
            return object()

    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        first = Probe(
            "first-critical",
            critical=True,
            deadline_seconds=0.01,
            behavior="success",
        )
        second = Probe(
            "second-critical",
            critical=True,
            deadline_seconds=1.0,
            behavior="success",
        )
        app = create_connector_gateway(
            dependency_probes=[first, second],
            authenticator=Authenticator(),
        )

        await app.startup()
        try:
            first.behavior = "failure"
            second.behavior = "block"
            await _wait_until(lambda: second.active_checks == 1)

            assert await _asgi_get_status(app, "/ready") == 503
            assert await _connector_websocket_outgoing(app) == [
                {
                    "type": "websocket.close",
                    "code": 1013,
                    "reason": "gateway_not_ready",
                }
            ]
        finally:
            await app.shutdown()

    asyncio.run(scenario())


def test_repeated_critical_failures_keep_monitoring_and_remain_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        probe = Probe(
            "sqlite",
            critical=True,
            deadline_seconds=0.01,
            behavior="failure",
        )
        runtime = ComponentRuntime("connector-gateway", dependency_probes=[probe])

        await runtime.startup()
        try:
            await _wait_until(lambda: probe.calls >= 4)
            snapshot = runtime.snapshot()

            assert snapshot["state"] == "READY"
            assert snapshot["ready"] is False
            assert snapshot["diagnostic"] == "BLOCKED"
            assert snapshot["dependencies"][0]["status"] == "FAILED"
            assert snapshot["error"] == {
                "category": "DEPENDENCY",
                "code": "DEPENDENCY_UNAVAILABLE",
                "retryable": True,
            }
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_repeated_timed_out_probes_never_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.001,
            raising=False,
        )
        probe = Probe(
            "sqlite",
            critical=True,
            deadline_seconds=0.005,
            behavior="block",
        )
        runtime = ComponentRuntime("business-api", dependency_probes=[probe])

        await runtime.startup()
        try:
            await _wait_until(lambda: probe.calls >= 3)

            assert runtime.snapshot()["ready"] is False
            assert probe.maximum_active_checks == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_shutdown_cancels_in_flight_probe_and_monitor_cannot_revive_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        refresh_interval = 0.005
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            refresh_interval,
            raising=False,
        )
        first = Probe(
            "first-critical",
            critical=True,
            deadline_seconds=1.0,
            behavior="success",
        )
        second = Probe(
            "second-critical",
            critical=True,
            deadline_seconds=1.0,
            behavior="success",
        )
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[first, second],
        )
        current = asyncio.current_task()
        tasks_before = {
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        }

        await runtime.startup()
        second.behavior = "block"
        await _wait_until(lambda: second.active_checks == 1)
        await runtime.shutdown()
        calls_at_stop = (first.calls, second.calls)

        second.behavior = "success"
        second.release.set()
        # This is a timing assertion: four known refresh intervals prove that
        # the cancelled monitor cannot schedule another dependency check.
        await asyncio.sleep(refresh_interval * 4)

        snapshot = runtime.snapshot()
        tasks_after = {
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        }
        dependency_names = [result["name"] for result in snapshot["dependencies"]]
        assert second.cancelled.is_set()
        assert (first.calls, second.calls) == calls_at_stop
        assert tasks_after <= tasks_before
        assert dependency_names == ["first-critical", "second-critical"]
        assert len(dependency_names) == len(set(dependency_names))
        assert snapshot["state"] == "STOPPED"
        assert snapshot["live"] is False
        assert snapshot["ready"] is False

    asyncio.run(scenario())


def test_fatal_component_failure_is_not_cleared_by_healthy_dependency_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.005,
            raising=False,
        )
        probe = Probe(
            "sqlite",
            critical=True,
            deadline_seconds=0.01,
            behavior="success",
        )
        runtime = ComponentRuntime("business-api", dependency_probes=[probe])

        await runtime.startup()
        try:
            runtime.mark_failed(RuntimeError("fatal-component-secret"))
            calls_at_failure = probe.calls
            await _wait_until(lambda: probe.calls >= calls_at_failure + 2)
            snapshot = runtime.snapshot()

            assert snapshot["state"] == "FAILED"
            assert snapshot["ready"] is False
            assert snapshot["diagnostic"] == "HEALTHY"
            assert snapshot["error"] == {
                "category": "INTERNAL",
                "code": "INTERNAL_ERROR",
                "retryable": False,
            }
            assert "fatal-component-secret" not in repr(snapshot)
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_optional_probe_failure_is_degraded_but_ready() -> None:
    async def scenario() -> None:
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[
                Probe(
                    "redis",
                    critical=False,
                    deadline_seconds=0.1,
                    behavior="failure",
                )
            ],
        )

        await runtime.startup()
        snapshot = runtime.snapshot()

        assert snapshot["state"] == "READY"
        assert snapshot["live"] is True
        assert snapshot["ready"] is True
        assert snapshot["diagnostic"] == "DEGRADED"
        assert snapshot["dependencies"][0]["criticality"] == "OPTIONAL"
        assert snapshot["dependencies"][0]["status"] == "FAILED"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_business_api_refreshes_critical_probe_without_overlap_and_degrades_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TenantResolver:
        def tenant_for_subject(self, _subject: str) -> None:
            return None

    class SecretResolver:
        def resolve(self, _reference: str) -> bytes:
            return b"x" * 32

    async def scenario() -> None:
        monkeypatch.setattr(
            runtime_module,
            "_DEPENDENCY_REFRESH_INTERVAL_SECONDS",
            0.01,
            raising=False,
        )
        probe = Probe(
            "postgresql",
            critical=True,
            deadline_seconds=0.05,
            behavior="success",
        )
        app = create_business_api(
            dependency_probes=[probe],
            identity_repository=object(),
            tenant_resolver=TenantResolver(),
            secret_resolver=SecretResolver(),
            settings={
                "signing_secret_ref": "unit-test",
                "access_ttl_seconds": 300,
                "refresh_ttl_seconds": 3600,
                "ticket_ttl_seconds": 60,
            },
        )

        await app.startup()
        assert app.snapshot()["ready"] is True
        probe.behavior = "failure"
        for _ in range(20):
            if app.snapshot()["ready"] is False:
                break
            await asyncio.sleep(0.01)

        ready_response = await app._ready()
        status_response = await app._status()

        assert ready_response.status_code == 503
        assert status_response == {
            "gateway_running": True,
            "gateway_state": "degraded",
            "auth_required": False,
            "auth_providers": [],
            "auth_flows": [],
            "overall": "degraded",
        }
        assert probe.calls >= 2
        assert probe.maximum_active_checks == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_startup_cancellation_cleans_probe_and_allows_shutdown() -> None:
    async def scenario() -> None:
        probe = Probe(
            "postgresql",
            critical=True,
            deadline_seconds=10.0,
            behavior="block",
        )
        runtime = ComponentRuntime(
            "business-api",
            dependency_probes=[probe],
        )
        current = asyncio.current_task()
        before = {
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        }

        startup = asyncio.create_task(runtime.startup())
        await probe.started.wait()
        startup.cancel()
        try:
            await startup
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("startup cancellation did not propagate")

        assert probe.cancelled.is_set()
        assert runtime.snapshot()["state"] == "FAILED"
        assert runtime.snapshot()["ready"] is False
        await runtime.shutdown()
        await asyncio.sleep(0)
        after = {
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        }
        assert after <= before

    asyncio.run(scenario())


def test_asgi_lifespan_startup_cancellation_propagates_without_double_failure() -> None:
    async def scenario() -> None:
        probe = Probe(
            "postgresql",
            critical=True,
            deadline_seconds=10.0,
            behavior="block",
        )
        app = create_business_api(dependency_probes=[probe])
        incoming = asyncio.Queue[dict[str, object]]()
        outgoing: list[dict[str, object]] = []
        await incoming.put({"type": "lifespan.startup"})

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            outgoing.append(message)

        task = asyncio.create_task(
            app(
                {"type": "lifespan", "asgi": {"version": "3.0"}},
                receive,
                send,
            )
        )
        await probe.started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert app.snapshot()["state"] == "FAILED"
        assert outgoing == []
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "factory",
    [
        create_business_api,
        create_connector_gateway,
        create_file_gateway,
    ],
)
def test_http_factory_accepts_dependency_probes(factory: object) -> None:
    async def scenario() -> None:
        app = factory(
            dependency_probes=[
                Probe(
                    "postgresql",
                    critical=True,
                    deadline_seconds=0.1,
                    behavior="failure",
                )
            ]
        )

        await app.startup()

        assert app.snapshot()["live"] is True
        assert app.snapshot()["ready"] is False
        await app.shutdown()

    asyncio.run(scenario())


def test_worker_factory_accepts_dependency_probes() -> None:
    async def scenario() -> None:
        worker = create_worker(
            dependency_probes=[
                Probe(
                    "postgresql",
                    critical=True,
                    deadline_seconds=0.1,
                    behavior="failure",
                )
            ]
        )

        await worker.start()

        assert worker.snapshot()["live"] is True
        assert worker.snapshot()["ready"] is False
        await worker.stop()

    asyncio.run(scenario())
