from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.cloud_api.domain import (
    Principal,
    WebSocketTicketAuthentication,
)
from hermes_cloud.modules.control.broker import OwnerControlBroker
from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
    ControlRpcError,
)
from hermes_cloud.modules.control.runtime import BrokeredControlRuntime

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
REFRESH_ID = UUID("33333333-3333-4333-8333-333333333333")
CLIENT_ID = "44444444-4444-4444-8444-444444444444"
CONTROL_CONNECTION_ID = "55555555-5555-4555-8555-555555555555"
CONNECTOR_CONNECTION_ID = "66666666-6666-4666-8666-666666666666"
REPLACEMENT_CONNECTION_ID = "77777777-7777-4777-8777-777777777777"
DEVICE_ID = "device-test"
SESSION_KEY = "session-root-1"
AGENT_ID = UUID("88888888-8888-4888-8888-888888888888")
SESSION_ID = UUID("99999999-9999-4999-8999-999999999999")
NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)


def _context() -> ControlRequestContext:
    return ControlRequestContext(
        authentication=WebSocketTicketAuthentication(
            principal=Principal(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                provider="basic",
                refresh_session_id=REFRESH_ID,
            ),
            connection_role="control",
            client_instance_id=CLIENT_ID,
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            profile="default",
            agent_id=AGENT_ID,
        ),
        connection_id=CONTROL_CONNECTION_ID,
    )


def test_runtime_advertises_the_exact_canonical_control_error_catalog() -> None:
    source = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "fixtures/repository_contracts/sources/mobile-control-v1.json"
        ).read_text(encoding="utf-8")
    )

    assert dict(BrokeredControlRuntime.error_codes) == source["error_codes"]


class _RouteResolver:
    async def resolve(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        assert context.authentication == _context().authentication
        return ControlConnectorRoute(
            tenant_id=str(TENANT_ID),
            device_id=DEVICE_ID,
        )


class _RevocableRouteResolver(_RouteResolver):
    def __init__(self) -> None:
        self.revoked = False
        self.calls = 0

    async def resolve(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        self.calls += 1
        if self.revoked:
            raise ControlRpcError(
                code=4202,
                message="authoritative live runtime unavailable",
            )
        return await super().resolve(context)


class _LoopbackSender:
    def __init__(self) -> None:
        self.broker: OwnerControlBroker | None = None
        self.connection_id = CONNECTOR_CONNECTION_ID
        self.requests: list[dict[str, object]] = []
        self.respond = True
        self.effect_started = True
        self.status_result: dict[str, object] | None = None

    async def send_control_request(
        self,
        request: Mapping[str, object],
    ) -> bool:
        copied = deepcopy(dict(request))
        self.requests.append(copied)
        if self.respond:
            assert self.broker is not None
            result = _result_for(
                operation=str(copied["operation"]),
                lease_id=(
                    copied["body"].get("lease_id")
                    if isinstance(copied["body"], dict)
                    else None
                ),
            )
            if copied["operation"] == "session.control.status" and self.status_result:
                result = dict(self.status_result)
            await self.broker.accept_control_response(
                identity=ConnectorIdentity(
                    tenant_id=str(TENANT_ID),
                    device_id=DEVICE_ID,
                ),
                connector_connection_id=self.connection_id,
                response={
                    "request_id": copied["request_id"],
                    "control_transport_id": copied["control_transport_id"],
                    "operation": copied["operation"],
                    "state": "succeeded",
                    "completed_at": "2026-07-31T02:00:01Z",
                    "result": result,
                },
            )
        return self.effect_started


def _result_for(
    *,
    operation: str,
    lease_id: object = "opaque-owner-lease",
) -> dict[str, object]:
    if operation == "control.transport.open":
        return {"attached": True, "connection_role": "control"}
    if operation in {"session.control.acquire", "session.control.renew"}:
        return {
            "lease_id": lease_id or "opaque-owner-lease",
            "expires_at_epoch_ms": 1_785_463_232_000,
            "control_revision": 3,
            "controller_kind": "mobile",
            "controller_label": "Hermes Mobile",
            "pending_input": None,
        }
    if operation == "session.control.release":
        return {"released": True, "control_revision": 4}
    if operation == "session.control.status":
        return {
            "controller_kind": "mobile",
            "controller_label": "Hermes Mobile",
            "control_revision": 4,
            "lease_expires_at_epoch_ms": 1_785_463_232_000,
            "pending_input": None,
        }
    if operation == "session.command.status":
        return {
            "status": "accepted",
            "client_request_id": "client-request-status",
            "client_turn_id": "client-turn-status",
            "server_turn_id": "server-turn-status",
        }
    if operation == "prompt.submit":
        return {
            "status": "queued",
            "client_request_id": "client-request-prompt",
            "client_turn_id": "client-turn-prompt",
            "server_turn_id": "server-turn-prompt",
        }
    if operation in {"session.interrupt", "session.steer"}:
        return {
            "status": "accepted",
            "client_request_id": f"client-request-{operation.rsplit('.', 1)[1]}",
        }
    if operation in {"approval.respond", "clarify.respond"}:
        return {
            "status": "accepted",
            "kind": operation.split(".", 1)[0],
            "request_id": f"pending-{operation.split('.', 1)[0]}",
            "client_request_id": f"client-request-{operation.split('.', 1)[0]}",
            "control_revision": 5,
        }
    if operation == "control.transport.close":
        return {"closed": True}
    raise AssertionError(operation)


async def _connected_runtime(
    *,
    timeout_seconds: float = 0.1,
) -> tuple[OwnerControlBroker, BrokeredControlRuntime, _LoopbackSender]:
    request_ids = iter(UUID(int=index) for index in range(1, 100))
    broker = OwnerControlBroker(
        control_transport_id_factory=lambda: UUID(
            "88888888-8888-4888-8888-888888888888"
        ),
    )
    sender = _LoopbackSender()
    sender.broker = broker
    await broker.connector_connected(
        identity=ConnectorIdentity(
            tenant_id=str(TENANT_ID),
            device_id=DEVICE_ID,
        ),
        connector_connection_id=CONNECTOR_CONNECTION_ID,
        sender=sender,
    )
    runtime = BrokeredControlRuntime(
        broker=broker,
        route_resolver=_RouteResolver(),
        request_id_factory=lambda: next(request_ids),
        now=lambda: NOW,
        timeout_seconds=timeout_seconds,
    )
    return broker, runtime, sender


@pytest.mark.asyncio
async def test_runtime_maps_one_control_connection_through_full_owner_control_lifecycle() -> (
    None
):
    broker, runtime, sender = await _connected_runtime()
    context = _context()

    await runtime.open(context=context)
    acquire = await runtime.execute(
        context=context,
        method="session.control.acquire",
        params={
            "session_id": str(SESSION_ID),
        },
    )
    lease_id = str(acquire["lease_id"])
    renewed = await runtime.execute(
        context=context,
        method="session.control.renew",
        params={
            "session_id": str(SESSION_ID),
            "lease_id": lease_id,
        },
    )
    released = await runtime.execute(
        context=context,
        method="session.control.release",
        params={
            "session_id": str(SESSION_ID),
            "lease_id": lease_id,
        },
    )
    status = await runtime.execute(
        context=context,
        method="session.control.status",
        params={"session_id": str(SESSION_ID)},
    )
    await runtime.close(context=context, reason="client_disconnected")

    assert renewed["lease_id"] == lease_id
    assert released == {"released": True, "control_revision": 4}
    assert status["controller_kind"] == "mobile"
    assert [request["operation"] for request in sender.requests] == [
        "control.transport.open",
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "control.transport.close",
    ]
    assert {request["control_transport_id"] for request in sender.requests} == {
        "88888888-8888-4888-8888-888888888888"
    }
    assert sender.requests[0]["body"] == {
        "principal_id": str(USER_ID),
        "client_instance_id": CLIENT_ID,
        "session_key": SESSION_KEY,
        "profile": "default",
    }
    assert sender.requests[1]["body"] == {}
    assert sender.requests[2]["body"] == {"lease_id": lease_id}
    assert sender.requests[3]["body"] == {"lease_id": lease_id}
    assert sender.requests[4]["body"] == {}
    assert sender.requests[5]["body"] == {"reason": "client_disconnected"}
    assert broker.control_transport_id_for(CONTROL_CONNECTION_ID) is None


@pytest.mark.asyncio
async def test_runtime_revalidates_authority_before_each_effect() -> None:
    request_ids = iter(UUID(int=index) for index in range(1, 100))
    broker = OwnerControlBroker(
        control_transport_id_factory=lambda: UUID(
            "88888888-8888-4888-8888-888888888888"
        ),
    )
    sender = _LoopbackSender()
    sender.broker = broker
    await broker.connector_connected(
        identity=ConnectorIdentity(
            tenant_id=str(TENANT_ID),
            device_id=DEVICE_ID,
        ),
        connector_connection_id=CONNECTOR_CONNECTION_ID,
        sender=sender,
    )
    resolver = _RevocableRouteResolver()
    runtime = BrokeredControlRuntime(
        broker=broker,
        route_resolver=resolver,
        request_id_factory=lambda: next(request_ids),
        now=lambda: NOW,
        timeout_seconds=0.1,
    )
    context = _context()
    await runtime.open(context=context)
    resolver.revoked = True

    with pytest.raises(ControlRpcError) as revoked:
        await runtime.execute(
            context=context,
            method="session.control.acquire",
            params={"session_id": str(SESSION_ID)},
        )

    assert revoked.value.code == 4202
    assert resolver.calls == 2
    assert [request["operation"] for request in sender.requests] == [
        "control.transport.open"
    ]


@pytest.mark.asyncio
async def test_runtime_normalizes_legacy_local_status_before_publication() -> None:
    _broker, runtime, sender = await _connected_runtime()
    sender.status_result = {
        "controller_kind": "local",
        "controller_label": "Hermes Desktop",
        "control_revision": 4,
        "lease_expires_at_epoch_ms": 0,
        "pending_input": None,
    }
    context = _context()
    await runtime.open(context=context)

    status = await runtime.execute(
        context=context,
        method="session.control.status",
        params={"session_id": str(SESSION_ID)},
    )

    assert status["controller_kind"] == "desktop"
    assert status["controller_label"] == "Hermes Desktop"


@pytest.mark.asyncio
async def test_request_id_is_memory_idempotent_and_changed_payload_conflicts() -> None:
    broker, runtime, sender = await _connected_runtime()
    context = _context()
    await runtime.open(context=context)
    request_id = "12121212-1212-4212-8212-121212121212"

    first = await broker.route_request(
        control_connection_id=CONTROL_CONNECTION_ID,
        request_id=request_id,
        operation="session.control.status",
        issued_at="2026-07-31T02:00:00Z",
        expires_at="2026-07-31T02:00:03Z",
        body={},
        timeout_seconds=0.1,
    )
    exact_retry = await broker.route_request(
        control_connection_id=CONTROL_CONNECTION_ID,
        request_id=request_id,
        operation="session.control.status",
        issued_at="2026-07-31T02:00:00Z",
        expires_at="2026-07-31T02:00:03Z",
        body={},
        timeout_seconds=0.1,
    )

    assert exact_retry == first
    assert [
        request["request_id"]
        for request in sender.requests
        if request["request_id"] == request_id
    ] == [request_id]

    with pytest.raises(ControlRpcError) as conflict:
        await broker.route_request(
            control_connection_id=CONTROL_CONNECTION_ID,
            request_id=request_id,
            operation="session.control.renew",
            issued_at="2026-07-31T02:00:00Z",
            expires_at="2026-07-31T02:00:03Z",
            body={"lease_id": "different-payload"},
            timeout_seconds=0.1,
        )

    assert conflict.value.code == 4207


@pytest.mark.asyncio
async def test_timeout_distinguishes_before_effect_from_effect_unknown() -> None:
    _broker, runtime, sender = await _connected_runtime(timeout_seconds=0.01)
    context = _context()
    await runtime.open(context=context)

    sender.respond = False
    sender.effect_started = False
    with pytest.raises(ControlRpcError) as before_effect:
        await runtime.execute(
            context=context,
            method="session.control.status",
            params={"session_id": str(SESSION_ID)},
        )
    assert before_effect.value.code == 4306

    sender.effect_started = True
    with pytest.raises(ControlRpcError) as effect_unknown:
        await runtime.execute(
            context=context,
            method="session.control.status",
            params={"session_id": str(SESSION_ID)},
        )
    assert effect_unknown.value.code == 4307


@pytest.mark.asyncio
async def test_connector_replacement_and_disconnect_fail_closed_and_cleanup() -> None:
    broker, runtime, _sender = await _connected_runtime()
    context = _context()
    await runtime.open(context=context)

    replacement = _LoopbackSender()
    replacement.broker = broker
    replacement.connection_id = REPLACEMENT_CONNECTION_ID
    await broker.connector_connected(
        identity=ConnectorIdentity(
            tenant_id=str(TENANT_ID),
            device_id=DEVICE_ID,
        ),
        connector_connection_id=REPLACEMENT_CONNECTION_ID,
        sender=replacement,
    )

    with pytest.raises(ControlRpcError) as stale:
        await runtime.execute(
            context=context,
            method="session.control.status",
            params={"session_id": str(SESSION_ID)},
        )
    assert stale.value.code == 4202
    assert replacement.requests == []

    await runtime.close(context=context, reason="client_disconnected")
    assert broker.control_transport_id_for(CONTROL_CONNECTION_ID) is None

    second_context = ControlRequestContext(
        authentication=context.authentication,
        connection_id="13131313-1313-4313-8313-131313131313",
    )
    await runtime.open(context=second_context)
    await broker.connector_disconnected(
        identity=ConnectorIdentity(
            tenant_id=str(TENANT_ID),
            device_id=DEVICE_ID,
        ),
        connector_connection_id=REPLACEMENT_CONNECTION_ID,
    )

    with pytest.raises(ControlRpcError) as disconnected:
        await runtime.execute(
            context=second_context,
            method="session.control.status",
            params={"session_id": str(SESSION_ID)},
        )
    assert disconnected.value.code == 4202
    await runtime.close(
        context=second_context,
        reason="client_disconnected",
    )
    assert broker.control_transport_id_for(second_context.connection_id) is None


def test_owner_control_runtime_has_no_persistence_dependency() -> None:
    import hermes_cloud.modules.control.broker as broker_module
    import hermes_cloud.modules.control.runtime as runtime_module

    imported: set[str] = set()
    for module in (broker_module, runtime_module):
        path = module.__file__
        assert path is not None
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )

    assert not any(
        forbidden in module_name.lower()
        for module_name in imported
        for forbidden in ("logging", "persistence", "sqlalchemy", "sqlite")
    )


@pytest.mark.asyncio
async def test_runtime_advertises_safe_mobile_subset_and_maps_exact_operation_bodies() -> (
    None
):
    _broker, runtime, sender = await _connected_runtime()
    context = _context()
    await runtime.open(context=context)
    expected_methods = (
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "session.command.status",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    )
    assert runtime.available_methods == expected_methods
    assert runtime.error_codes["command_unknown"] == 4210
    assert runtime.error_codes["invalid_pending_response"] == 4213

    cases = (
        (
            "session.command.status",
            {
                "session_id": str(SESSION_ID),
                "method": "approval.respond",
                "client_request_id": "client-request-status",
            },
            {
                "method": "approval.respond",
                "client_request_id": "client-request-status",
            },
        ),
        (
            "prompt.submit",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-prompt",
                "client_turn_id": "client-turn-prompt",
                "text": "Queue this next turn",
            },
            {
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-prompt",
                "client_turn_id": "client-turn-prompt",
                "text": "Queue this next turn",
            },
        ),
        (
            "session.interrupt",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-interrupt",
            },
            {
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-interrupt",
            },
        ),
        (
            "session.steer",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-steer",
                "text": "Focus on the failing assertion",
            },
            {
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-steer",
                "text": "Focus on the failing assertion",
            },
        ),
        (
            "approval.respond",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-approval",
                "request_id": "pending-approval",
                "choice": "allow_once",
            },
            {
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-approval",
                "request_id": "pending-approval",
                "choice": "allow_once",
            },
        ),
        (
            "clarify.respond",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-clarify",
                "request_id": "pending-clarify",
                "choice_id": "choice-1",
            },
            {
                "lease_id": "opaque-owner-lease",
                "client_request_id": "client-request-clarify",
                "request_id": "pending-clarify",
                "choice_id": "choice-1",
            },
        ),
    )
    for method, params, _expected in cases:
        await runtime.execute(context=context, method=method, params=params)

    assert [request["operation"] for request in sender.requests[1:]] == [
        method for method, _params, _expected in cases
    ]
    assert [request["body"] for request in sender.requests[1:]] == [
        expected for _method, _params, expected in cases
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "expected_code"),
    [
        (
            "prompt.submit",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "lease",
                "client_request_id": "request",
                "client_turn_id": "turn",
            },
            -32602,
        ),
        (
            "session.interrupt",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "lease",
                "client_request_id": "request",
                "extra": True,
            },
            -32602,
        ),
        (
            "approval.respond",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "lease",
                "client_request_id": "request",
                "request_id": "pending",
                "choice": "owner_internal_allow",
            },
            4213,
        ),
        (
            "clarify.respond",
            {
                "session_id": str(SESSION_ID),
                "lease_id": "lease",
                "client_request_id": "request",
                "request_id": "pending",
                "choice_id": "choice",
                "other_text": "both are forbidden",
            },
            4213,
        ),
        (
            "session.command.status",
            {"session_id": str(SESSION_ID), "client_request_id": 7},
            -32602,
        ),
    ],
)
async def test_runtime_rejects_invalid_action_params_before_broker(
    method: str,
    params: dict[str, object],
    expected_code: int,
) -> None:
    _broker, runtime, sender = await _connected_runtime()
    context = _context()
    await runtime.open(context=context)

    with pytest.raises(ControlRpcError) as rejected:
        await runtime.execute(context=context, method=method, params=params)

    assert rejected.value.code == expected_code
    assert len(sender.requests) == 1


@pytest.mark.asyncio
async def test_public_control_rpc_uses_only_stable_session_id() -> None:
    _broker, runtime, sender = await _connected_runtime()
    context = _context()
    await runtime.open(context=context)

    result = await runtime.execute(
        context=context,
        method="session.control.status",
        params={"session_id": str(SESSION_ID)},
    )

    assert result["controller_kind"] == "mobile"
    assert sender.requests[-1]["body"] == {}

    with pytest.raises(ControlRpcError) as legacy_host_identity:
        await runtime.execute(
            context=context,
            method="session.control.status",
            params={"session_key": SESSION_KEY},
        )
    assert legacy_host_identity.value.code == -32602

    with pytest.raises(ControlRpcError) as wrong_stable_id:
        await runtime.execute(
            context=context,
            method="session.control.status",
            params={"session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        )
    assert wrong_stable_id.value.code == 4212
