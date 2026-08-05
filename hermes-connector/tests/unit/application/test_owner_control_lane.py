from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

import pytest

from hermes_connector.domain.owner_control import OwnerControlRequest

NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
TRANSPORT_1 = UUID("11111111-1111-4111-8111-111111111111")
TRANSPORT_2 = UUID("22222222-2222-4222-8222-222222222222")
TRANSPORT_3 = UUID("44444444-4444-4444-8444-444444444444")
CLIENT_ID = "33333333-3333-4333-8333-333333333333"


def test_owner_control_lane_module_exists() -> None:
    try:
        module = importlib.import_module(
            "hermes_connector.application.owner_control_lane"
        )
    except ModuleNotFoundError:
        pytest.fail("OwnerControlLane is not implemented")

    assert hasattr(module, "OwnerControlLane")
    assert hasattr(module, "OwnerControlScope")


def _module():
    return importlib.import_module("hermes_connector.application.owner_control_lane")


def _request(
    operation: str,
    body: Mapping[str, object],
    *,
    request_id: str,
    transport_id: UUID = TRANSPORT_1,
    expires_at: datetime | None = None,
) -> OwnerControlRequest:
    return OwnerControlRequest(
        request_id=UUID(request_id),
        control_transport_id=transport_id,
        operation=operation,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=expires_at or NOW + timedelta(seconds=3),
        body=MappingProxyType(dict(body)),
    )


def _open_request(
    *,
    transport_id: UUID = TRANSPORT_1,
    request_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
) -> OwnerControlRequest:
    return _request(
        "control.transport.open",
        {
            "principal_id": "principal-1",
            "client_instance_id": CLIENT_ID,
            "session_key": "durable-root-1",
            "profile": "default",
        },
        request_id=request_id,
        transport_id=transport_id,
    )


class _Channel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID, Mapping[str, object], float]] = []
        self.closed = 0
        self.failure: BaseException | None = None

    async def execute(
        self,
        *,
        operation: str,
        request_id: UUID,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        self.calls.append((operation, request_id, body, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        if operation in {"session.control.acquire", "session.control.renew"}:
            return {
                "lease_id": "fixture-lease-secret-not-real-0001",
                "expires_at_epoch_ms": 1785463232000,
                "control_revision": 3,
                "controller_kind": "mobile",
                "controller_label": "Hermes Mobile",
                "pending_input": None,
            }
        if operation == "session.control.release":
            return {"released": True, "control_revision": 4}
        if operation == "session.command.status":
            return {
                "status": "accepted",
                "client_request_id": "request-status",
            }
        if operation == "prompt.submit":
            return {
                "status": "queued",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "server_turn_id": "server-turn-prompt",
            }
        if operation in {"session.interrupt", "session.steer"}:
            return {
                "status": "accepted",
                "client_request_id": f"request-{operation.rsplit('.', 1)[1]}",
            }
        if operation in {"approval.respond", "clarify.respond"}:
            kind = operation.split(".", 1)[0]
            return {
                "status": "accepted",
                "kind": kind,
                "request_id": f"pending-{kind}",
                "client_request_id": f"request-{kind}",
                "control_revision": 7,
            }
        return {
            "controller_kind": "desktop",
            "controller_label": "Hermes Desktop",
            "control_revision": 4,
            "lease_expires_at_epoch_ms": 0,
            "pending_input": None,
        }

    async def close(self) -> None:
        self.closed += 1


class _Factory:
    def __init__(self) -> None:
        self.channels: dict[UUID, _Channel] = {}
        self.scopes: list[object] = []

    async def open(
        self,
        *,
        scope: object,
        request_id: UUID,
        timeout_seconds: float,
    ) -> _Channel:
        self.scopes.append(scope)
        channel = _Channel()
        self.channels[scope.control_transport_id] = channel
        return channel


class _ConcurrencyProbe:
    def __init__(self, expected_active: int) -> None:
        self.expected_active = expected_active
        self.active = 0
        self.maximum_active = 0
        self.entered_count = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()


class _BlockingChannel(_Channel):
    def __init__(self, probe: _ConcurrencyProbe) -> None:
        super().__init__()
        self._probe = probe

    async def execute(
        self,
        *,
        operation: str,
        request_id: UUID,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        self._probe.active += 1
        self._probe.entered_count += 1
        self._probe.maximum_active = max(
            self._probe.maximum_active,
            self._probe.active,
        )
        if self._probe.active == self._probe.expected_active:
            self._probe.entered.set()
        try:
            await self._probe.release.wait()
            return await super().execute(
                operation=operation,
                request_id=request_id,
                body=body,
                timeout_seconds=timeout_seconds,
            )
        finally:
            self._probe.active -= 1


class _BlockingFactory(_Factory):
    def __init__(self, probe: _ConcurrencyProbe) -> None:
        super().__init__()
        self._probe = probe

    async def open(
        self,
        *,
        scope: object,
        request_id: UUID,
        timeout_seconds: float,
    ) -> _Channel:
        self.scopes.append(scope)
        channel = _BlockingChannel(self._probe)
        self.channels[scope.control_transport_id] = channel
        return channel


@pytest.mark.asyncio
async def test_open_binds_immutable_scope_and_reuses_exact_channel() -> None:
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)

    opened = await lane.process(_open_request())
    status = await lane.process(
        _request(
            "session.control.status",
            {},
            request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
    )

    scope = factory.scopes[0]
    assert scope.control_transport_id == TRANSPORT_1
    assert scope.principal_id == "principal-1"
    assert str(scope.client_instance_id) == CLIENT_ID
    assert scope.session_key == "durable-root-1"
    assert scope.profile == "default"
    assert opened.result == {"attached": True, "connection_role": "control"}
    assert status.result["controller_kind"] == "desktop"
    assert len(factory.channels[TRANSPORT_1].calls) == 1


@pytest.mark.asyncio
async def test_request_id_is_in_memory_idempotent_and_conflicts_on_new_body() -> None:
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)
    await lane.process(_open_request())
    request = _request(
        "session.control.status",
        {},
        request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )

    first = await lane.process(request)
    repeated = await lane.process(request)
    conflict = await lane.process(
        _request(
            "session.control.acquire",
            {},
            request_id=str(request.request_id),
        )
    )

    assert repeated is first
    assert len(factory.channels[TRANSPORT_1].calls) == 1
    assert conflict.state == "failed"
    assert conflict.error == {
        "code": 4207,
        "reason": "request_id_payload_conflict",
    }


@pytest.mark.asyncio
async def test_second_open_normalizes_internal_idempotency_conflict_for_cloud() -> None:
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)

    await lane.process(_open_request())
    conflict = await lane.process(
        replace(
            _open_request(),
            request_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        )
    )

    assert conflict.state == "failed"
    assert conflict.error == {
        "code": 4207,
        "reason": "request_id_payload_conflict",
    }


@pytest.mark.asyncio
async def test_expired_request_fails_before_opening_or_sending() -> None:
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)

    response = await lane.process(replace(_open_request(), expires_at=NOW))

    assert response.state == "failed"
    assert response.error == {
        "code": 4306,
        "reason": "deadline_exceeded_before_effect",
    }
    assert factory.scopes == []


@pytest.mark.asyncio
async def test_effect_unknown_is_returned_once_without_automatic_replay() -> None:
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)
    await lane.process(_open_request())
    channel = factory.channels[TRANSPORT_1]
    channel.failure = module.OwnerControlOutcomeUnknown()
    request = _request(
        "session.control.renew",
        {"lease_id": "fixture-lease-secret-not-real-0001"},
        request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )

    first = await lane.process(request)
    repeated = await lane.process(request)

    assert first.state == "unknown"
    assert first.error == {"code": 4307, "reason": "effect_unknown"}
    assert repeated is first
    assert len(channel.calls) == 1


@pytest.mark.asyncio
async def test_close_all_closes_every_live_transport_once() -> None:
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)
    await lane.process(_open_request())
    await lane.process(
        _open_request(
            transport_id=TRANSPORT_2,
            request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
    )

    await lane.close_all()
    await lane.close_all()

    assert {channel.closed for channel in factory.channels.values()} == {1}


@pytest.mark.asyncio
async def test_same_transport_requests_execute_in_fifo_order() -> None:
    module = _module()
    probe = _ConcurrencyProbe(expected_active=1)
    factory = _BlockingFactory(probe)
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)
    await lane.process(_open_request())
    first = asyncio.create_task(
        lane.process(
            _request(
                "session.control.status",
                {},
                request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            )
        )
    )
    await probe.entered.wait()
    second = asyncio.create_task(
        lane.process(
            _request(
                "session.control.status",
                {},
                request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            )
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert probe.entered_count == 1

    probe.release.set()
    await asyncio.gather(first, second)
    calls = factory.channels[TRANSPORT_1].calls
    assert [call[1] for call in calls] == [
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    ]


@pytest.mark.asyncio
async def test_parallel_transport_execution_is_bounded() -> None:
    module = _module()
    probe = _ConcurrencyProbe(expected_active=2)
    factory = _BlockingFactory(probe)
    lane = module.OwnerControlLane(
        factory=factory,
        utc_now=lambda: NOW,
        max_parallel_transports=2,
    )
    await lane.process(_open_request(transport_id=TRANSPORT_1))
    await lane.process(
        _open_request(
            transport_id=TRANSPORT_2,
            request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
    )
    await lane.process(
        _open_request(
            transport_id=TRANSPORT_3,
            request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )
    )
    tasks = [
        asyncio.create_task(
            lane.process(
                _request(
                    "session.control.status",
                    {},
                    transport_id=transport_id,
                    request_id=request_id,
                )
            )
        )
        for transport_id, request_id in (
            (TRANSPORT_1, "dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            (TRANSPORT_2, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            (TRANSPORT_3, "ffffffff-ffff-4fff-8fff-ffffffffffff"),
        )
    ]

    await probe.entered.wait()
    await asyncio.sleep(0)
    assert probe.active == 2
    assert probe.entered_count == 2

    probe.release.set()
    await asyncio.gather(*tasks)
    assert probe.maximum_active == 2
    assert probe.entered_count == 3


@pytest.mark.asyncio
async def test_safe_mobile_actions_are_forwarded_once_on_the_bound_owner_channel() -> (
    None
):
    module = _module()
    factory = _Factory()
    lane = module.OwnerControlLane(factory=factory, utc_now=lambda: NOW)
    await lane.process(_open_request())
    cases = (
        (
            "session.command.status",
            {
                "method": "approval.respond",
                "client_request_id": "request-status",
            },
        ),
        (
            "prompt.submit",
            {
                "lease_id": "lease",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "text": "Queue this turn",
            },
        ),
        (
            "session.interrupt",
            {"lease_id": "lease", "client_request_id": "request-interrupt"},
        ),
        (
            "session.steer",
            {
                "lease_id": "lease",
                "client_request_id": "request-steer",
                "text": "Focus on the first failure",
            },
        ),
        (
            "approval.respond",
            {
                "lease_id": "lease",
                "client_request_id": "request-approval",
                "request_id": "pending-approval",
                "choice": "allow_once",
            },
        ),
        (
            "clarify.respond",
            {
                "lease_id": "lease",
                "client_request_id": "request-clarify",
                "request_id": "pending-clarify",
                "choice_id": "choice-1",
            },
        ),
    )

    for index, (operation, body) in enumerate(cases, start=4):
        response = await lane.process(
            _request(
                operation,
                body,
                request_id=f"00000000-0000-4000-8000-{index:012d}",
            )
        )
        assert response.state == "succeeded"

    assert [call[0] for call in factory.channels[TRANSPORT_1].calls] == [
        operation for operation, _body in cases
    ]
