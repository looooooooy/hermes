from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_cloud.adapters.connector_asgi import ASGIConnectorConnection
from hermes_cloud.application.connector_gateway import (
    ConnectorGatewaySettings,
)
from hermes_cloud.domain.connector_gateway import (
    ConnectorAuthenticationExpired,
    ConnectorAuthorizationRevoked,
    ConnectorAuthorizationSuspended,
    ConnectorDisconnected,
    ConnectorIdentity,
    ConnectorObserverRejected,
)
from hermes_cloud.entrypoints.connector_gateway import create_app
from hermes_cloud.modules.control.domain import ControlConnectorRoute
from hermes_cloud.modules.control.gateway import GatewayOwnerControlRouter


class FakeAuthenticator:
    def __init__(
        self,
        *,
        tenant_id: str = "tenant-test",
        device_id: str = "device-test",
    ) -> None:
        self.identity = SimpleNamespace(
            tenant_id=tenant_id,
            device_id=device_id,
        )
        self.tokens: list[str] = []

    async def authenticate(self, bearer_token: str):
        self.tokens.append(bearer_token)
        return self.identity


class LifecycleOnHeartbeatAuthenticator(FakeAuthenticator):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.revalidations = 0

    async def revalidate(self, _identity: object) -> None:
        self.revalidations += 1
        raise self.error


class FakeResumeResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def resolve(
        self,
        identity: object,
        position: object,
        **_binding: object,
    ):
        self.calls.append((identity, position))
        return SimpleNamespace(
            decision="resumed",
            next_connector_sequence=7,
            next_cloud_sequence=11,
            handshake_disposition="advance",
        )


class RecordingTransportCursorAuthority:
    def __init__(
        self,
        events: list[str],
        *,
        activation_failure: Exception | None = None,
        confirmation_failure: Exception | None = None,
        disconnect_failures: int = 0,
        resume_decision: str = "fresh",
        next_connector_sequence: int = 0,
        next_cloud_sequence: int = 0,
        handshake_disposition: str = "advance",
    ) -> None:
        self.events = events
        self.activation_failure = activation_failure
        self.confirmation_failure = confirmation_failure
        self.disconnect_failures = disconnect_failures
        self.resume_decision = resume_decision
        self.next_connector_sequence = next_connector_sequence
        self.next_cloud_sequence = next_cloud_sequence
        self.handshake_disposition = handshake_disposition
        self.disconnect_attempts = 0
        self.preparations: list[dict[str, object]] = []
        self.confirmations: list[dict[str, object]] = []
        self.activations: list[dict[str, object]] = []
        self.commits: list[dict[str, object]] = []
        self.commit_recorded = asyncio.Event()

    async def resolve(self, *_args: object, **_kwargs: object):
        return SimpleNamespace(
            decision=self.resume_decision,
            next_connector_sequence=self.next_connector_sequence,
            next_cloud_sequence=self.next_cloud_sequence,
            handshake_disposition=self.handshake_disposition,
        )

    async def activate_session(self, **_binding: object) -> None:
        self.activations.append(dict(_binding))
        self.events.append("authority.activate")
        if self.activation_failure is not None:
            raise self.activation_failure

    async def prepare_session(self, **_binding: object) -> None:
        self.preparations.append(dict(_binding))
        self.events.append("authority.prepare")
        if self.activation_failure is not None:
            raise self.activation_failure

    async def confirm_session(self, **_binding: object) -> None:
        self.confirmations.append(dict(_binding))
        self.events.append("authority.confirm")
        if self.confirmation_failure is not None:
            raise self.confirmation_failure

    async def abort_session(self, **_binding: object) -> None:
        self.events.append("authority.abort")

    async def commit_cursors(self, **_binding: object) -> None:
        self.commits.append(dict(_binding))
        self.commit_recorded.set()
        self.events.append("authority.commit")

    async def disconnect_session(self, **_binding: object) -> None:
        self.disconnect_attempts += 1
        self.events.append(f"authority.disconnect.{self.disconnect_attempts}")
        if self.disconnect_attempts <= self.disconnect_failures:
            raise RuntimeError("secret-frame-body-must-not-leak")


class FakeCommandRouter:
    def __init__(self) -> None:
        self.deliveries: asyncio.Queue[object] = asyncio.Queue()
        self.connected: list[dict[str, object]] = []
        self.disconnected: list[dict[str, object]] = []
        self.dispatched: list[tuple[str, str]] = []
        self.responses: list[object] = []

    async def connector_connected(self, **binding: object) -> None:
        self.connected.append(dict(binding))

    async def connector_disconnected(self, **binding: object) -> None:
        self.disconnected.append(dict(binding))

    async def wait_for_delivery(self, **_binding: object):
        return await self.deliveries.get()

    async def reserve_delivery(
        self,
        *,
        identity: object,
        command_id: str,
        message_id: str,
        **_binding: object,
    ):
        self.dispatched.append((identity.tenant_id, command_id))
        return SimpleNamespace(
            command_id=command_id,
            message_id=message_id,
            sent_at="2026-07-31T00:00:00.000Z",
            payload={
                "command_id": command_id,
                "connector_instance_id": ("11111111-1111-4111-8111-111111111111"),
                "client_instance_id": ("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                "session_key": "durable-root-1",
                "profile": "default",
                "client_request_id": "req-client-01",
                "method": "prompt.submit",
                "params": {
                    "runtime_session_id": "runtime-7",
                    "runtime_generation": "runtime-test",
                    "client_turn_id": "turn-client-01",
                    "text": "Continue the current task.",
                },
                "issued_at": "2026-07-31T00:00:00Z",
                "expires_at": "2026-07-31T00:05:00Z",
                "revision": 1,
            },
        )

    async def connector_heartbeat(
        self,
        **_binding: object,
    ) -> None:
        return None

    async def accept_connector_response(
        self,
        *,
        envelope: object,
        **_binding: object,
    ) -> None:
        self.responses.append(envelope)


class RecordingCommandRouter(FakeCommandRouter):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def connector_connected(self, **binding: object) -> None:
        self.events.append("router.connected")
        await super().connector_connected(**binding)


class FakeObserverIngress:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        event_failure: Exception | None = None,
    ) -> None:
        self.failure = failure
        self.event_failure = event_failure
        self.snapshots: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    async def accept_snapshot(self, **call: object) -> None:
        self.snapshots.append(dict(call))
        if self.failure is not None:
            raise self.failure

    async def accept_event(self, **call: object) -> None:
        self.events.append(dict(call))
        if self.event_failure is not None:
            raise self.event_failure
        if self.failure is not None:
            raise self.failure


class FakeSessionCatalogIngress:
    def __init__(self) -> None:
        self.pages: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.pending_receipt = None
        self.dispatch_connection_id: str | None = None
        self.reservations: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []
        self.settlements: list[dict[str, object]] = []
        self.dispatch_attempt = 0

    async def accept_snapshot_page(self, **call: object):
        self.pages.append(dict(call))
        envelope = call["envelope"]
        payload = call["payload"]
        if not payload.is_last:
            return None
        self.dispatch_attempt += 1
        receipt = SimpleNamespace(
            catalog_message_id=envelope.message_id,
            message_id=(
                f"99999999-9999-4999-8999-{self.dispatch_attempt:012d}"
            ),
            message_type="session.catalog.ack",
            sequence=call.get("expected_next_cloud_sequence", 0),
            sent_at="2026-08-01T04:00:00.000Z",
            payload={
                "profile": payload.profile,
                "runtime_generation": payload.runtime_generation,
                "acked_message_id": envelope.message_id,
                "acked_payload_digest": hashlib.sha256(
                    json.dumps(
                        envelope.payload,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                "acked_connector_sequence": envelope.sequence,
                "ack_kind": "snapshot_committed",
                "snapshot_id": payload.snapshot_id,
                "catalog_revision": payload.catalog_revision,
                "page_index": payload.page_index,
                "is_last": True,
            },
        )
        self.pending_receipt = receipt
        self.dispatch_connection_id = str(call["connection_id"])
        return receipt

    async def accept_snapshot_page_and_advance(self, **call: object):
        return await self.accept_snapshot_page(**call)

    async def accept_event(self, **call: object):
        self.events.append(dict(call))
        raise AssertionError("event not expected")

    async def accept_event_and_advance(self, **call: object):
        return await self.accept_event(**call)

    async def next_pending_receipt(self, **call: object) -> str | None:
        if (
            self.pending_receipt is None
            or self.dispatch_connection_id == call["connection_id"]
        ):
            return None
        return str(self.pending_receipt.catalog_message_id)

    async def reserve_pending_receipt_and_advance(self, **call: object):
        self.reservations.append(dict(call))
        assert self.pending_receipt is not None
        self.dispatch_attempt += 1
        self.pending_receipt = SimpleNamespace(
            **{
                **vars(self.pending_receipt),
                "message_id": (
                    f"99999999-9999-4999-8999-{self.dispatch_attempt:012d}"
                ),
                "sequence": call["expected_next_cloud_sequence"],
            }
        )
        self.dispatch_connection_id = str(call["connection_id"])
        return self.pending_receipt

    async def mark_receipt_sent(self, **call: object) -> None:
        assert self.dispatch_connection_id == call["connection_id"]
        assert self.pending_receipt is not None
        assert self.pending_receipt.message_id == call["message_id"]
        self.sent.append(dict(call))

    async def confirm_receipts_through_cursor(self, **call: object) -> int:
        self.settlements.append(dict(call))
        if (
            self.pending_receipt is not None
            and self.dispatch_connection_id == call["connection_id"]
            and self.pending_receipt.sequence
            < call["durable_next_inbound_sequence"]
        ):
            self.pending_receipt = None
            return 1
        return 0


class LegacyNonAtomicSessionCatalogIngress:
    async def accept_snapshot_page(self, **_call: object):
        return None

    async def accept_event(self, **_call: object):
        raise AssertionError("event not expected")

class FakeObserverReceiptRouter:
    def __init__(self, *, pending: Iterable[str] = ()) -> None:
        self.staged: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []
        self.confirmed: list[dict[str, object]] = []
        self.pending = list(pending)
        self.redeliveries: list[dict[str, object]] = []

    async def stage_and_reserve(self, **call: object):
        self.staged.append(dict(call))
        return SimpleNamespace(
            observer_message_id=call["observer_message_id"],
            message_id="66666666-6666-4666-8666-666666666666",
            message_type=call["receipt_type"],
            sequence=call["sequence"],
            sent_at="2026-08-01T04:00:00.000Z",
            payload=call["payload"],
        )

    async def next_pending(self, **_call: object) -> str | None:
        return self.pending.pop(0) if self.pending else None

    async def reserve_redelivery(self, **call: object):
        self.redeliveries.append(dict(call))
        return SimpleNamespace(
            observer_message_id=call["observer_message_id"],
            message_id="77777777-7777-4777-8777-777777777777",
            message_type="stream.ack",
            sequence=call["sequence"],
            sent_at="2026-08-01T04:00:01.000Z",
            payload={
                "observer_message_id": call["observer_message_id"],
                "payload_digest": "a" * 64,
                "connector_sequence": 1,
                "observer_message_type": "session.event",
                "profile": "default",
                "session_key": "session-root-1",
                "runtime_generation": "runtime-test",
                "runtime_session_id": "runtime-session-1",
                "event_sequence": 6,
                "committed_at": "2026-08-01T04:00:00.000Z",
            },
        )

    async def mark_sent(self, **call: object) -> None:
        self.sent.append(dict(call))

    async def confirm_through_cursor(self, **call: object) -> int:
        self.confirmed.append(dict(call))
        return 0


class FailingStageObserverReceiptRouter(FakeObserverReceiptRouter):
    async def stage_and_reserve(self, **call: object):
        self.staged.append(dict(call))
        raise RuntimeError("receipt outbox unavailable")


class FakeObserverSubscriptionRouter:
    def __init__(self) -> None:
        self.deliveries: asyncio.Queue[object] = asyncio.Queue()
        self.connected: list[dict[str, object]] = []
        self.disconnected: list[dict[str, object]] = []
        self.reserved: list[dict[str, object]] = []
        self.by_request: dict[str, object] = {}

    async def connector_connected(self, **binding: object) -> None:
        self.connected.append(dict(binding))

    async def connector_disconnected(self, **binding: object) -> None:
        self.disconnected.append(dict(binding))

    async def wait_for_subscription_intent(self, **_binding: object):
        delivery = await self.deliveries.get()
        self.by_request[delivery.request_id] = delivery
        return delivery

    async def reserve_subscription_intent(self, **binding: object):
        self.reserved.append(dict(binding))
        delivery = self.by_request[str(binding["request_id"])]
        delivery.observer_contract = binding["observer_contract"]
        delivery.wire_message_type = binding["wire_message_type"]
        delivery.wire_payload_digest = binding["wire_payload_digest"]
        return delivery


class ObserverAuthenticator:
    async def authenticate(self, _bearer_token: str) -> ConnectorIdentity:
        return ConnectorIdentity(
            tenant_id="tenant-test",
            device_id="device-test",
            agent_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scopes=("session.observe",),
            legacy_seed=False,
        )


class FailingSecondRouter(FakeCommandRouter):
    def __init__(self, *, block: bool) -> None:
        super().__init__()
        self.block = block

    async def connector_connected(self, **binding: object) -> None:
        self.connected.append(dict(binding))
        if self.block:
            await asyncio.Event().wait()
        raise RuntimeError("second router registration failed")


class ControlledSleep:
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.called.set()
        await self.release.wait()
        self.release.clear()


class BlockingAuthenticator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def authenticate(self, _bearer_token: str):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RejectingFrameDecoder:
    def __init__(self) -> None:
        self.frames: list[object] = []

    def decode_connector_frame(self, raw: object):
        self.frames.append(raw)
        raise ValueError("frame rejected by injected decoder")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("negotiation_timeout_seconds", float("nan")),
        ("negotiation_timeout_seconds", float("inf")),
        ("io_timeout_seconds", float("nan")),
        ("io_timeout_seconds", float("inf")),
        ("heartbeat_timeout_seconds", float("nan")),
        ("heartbeat_timeout_seconds", float("inf")),
        ("resume_timeout_seconds", float("nan")),
        ("resume_timeout_seconds", float("inf")),
        ("transport_ownership_lease_seconds", float("nan")),
        ("transport_ownership_lease_seconds", float("inf")),
    ),
)
def test_gateway_settings_reject_nonfinite_deadlines(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="must be finite and positive"):
        ConnectorGatewaySettings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("heartbeat_interval_ms", 20_000.0),
        ("heartbeat_interval_ms", True),
        ("max_in_flight", 64.0),
        ("max_in_flight", True),
    ),
)
def test_gateway_settings_require_strict_protocol_integers(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        ConnectorGatewaySettings(**{field: value})


def test_transport_ownership_lease_exceeds_negotiation_and_heartbeat_windows() -> None:
    settings = ConnectorGatewaySettings()
    required_window = max(
        settings.negotiation_timeout_seconds
        + settings.io_timeout_seconds
        + (2 * settings.router_timeout_seconds),
        settings.heartbeat_timeout_seconds
        + (settings.heartbeat_interval_ms / 1000)
        + settings.io_timeout_seconds,
    )
    assert settings.transport_ownership_lease_seconds > required_window

    with pytest.raises(ValueError, match="ownership lease"):
        ConnectorGatewaySettings(transport_ownership_lease_seconds=required_window)


def _hello(
    *,
    tenant_id: str = "tenant-test",
    device_id: str = "device-test",
    required: Iterable[str] = ("session.observe",),
    optional: Iterable[str] = ("session.control",),
) -> str:
    return json.dumps(
        {
            "contract_version": 1,
            "message_id": "22222222-2222-4222-8222-222222222222",
            "message_type": "connector.hello",
            "tenant_id": tenant_id,
            "device_id": device_id,
            "sequence": 0,
            "sent_at": "2026-07-31T00:00:00Z",
            "payload": {
                "connector_instance_id": ("11111111-1111-4111-8111-111111111111"),
                "connector_version": "1.0.0",
                "runtime_generation": "runtime-test",
                "required_capabilities": list(required),
                "optional_capabilities": list(optional),
                "resume": {
                    "mode": "fresh",
                    "next_outbound_sequence": 0,
                    "next_inbound_sequence": 0,
                },
            },
        },
        separators=(",", ":"),
    )


def _replace_envelope(
    text: str,
    *,
    message_type: str | None = None,
    tenant_id: str | None = None,
    device_id: str | None = None,
    sequence: int | None = None,
    payload: dict[str, object] | None = None,
) -> str:
    envelope = json.loads(text)
    if message_type is not None:
        envelope["message_type"] = message_type
    if tenant_id is not None:
        envelope["tenant_id"] = tenant_id
    if device_id is not None:
        envelope["device_id"] = device_id
    if sequence is not None:
        envelope["sequence"] = sequence
    if payload is not None:
        envelope["payload"] = payload
    return json.dumps(envelope, separators=(",", ":"))


def _heartbeat(
    *,
    connection_id: str,
    sequence: int,
    next_outbound_sequence: int,
    next_inbound_sequence: int,
) -> str:
    return _replace_envelope(
        _hello(),
        message_type="connector.heartbeat",
        sequence=sequence,
        payload={
            "connection_id": connection_id,
            "sender_role": "connector",
            "observed_at": "2026-07-31T00:00:20Z",
            "next_outbound_sequence": next_outbound_sequence,
            "next_inbound_sequence": next_inbound_sequence,
            "session_state": "active",
        },
    )


def _observer_frame(
    *,
    message_type: str,
    sequence: int,
    fixture: str,
    event_sequence: int | None = None,
) -> str:
    payload = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "fixtures/repository_contracts/fixtures"
            / fixture
        ).read_text(encoding="utf-8")
    )
    payload["runtime_generation"] = "runtime-test"
    if event_sequence is not None:
        payload["event_sequence"] = event_sequence
    return _replace_envelope(
        _hello(optional=()),
        message_type=message_type,
        sequence=sequence,
        payload=payload,
    )


def _catalog_snapshot_frame(*, sequence: int, is_last: bool) -> str:
    return _replace_envelope(
        _hello(required=("session.catalog.v1",), optional=()),
        message_type="session.catalog.snapshot.page",
        sequence=sequence,
        payload={
            "profile": "default",
            "runtime_generation": "runtime-test",
            "snapshot_id": "88888888-8888-4888-8888-888888888888",
            "catalog_revision": 7,
            "page_index": 0,
            "is_last": is_last,
            "sessions": [
                {
                    "session_key": "session-root-1",
                    "surface": "hermes-cli",
                    "authority_revision": 1,
                    "available_actions": ["prompt.submit"],
                }
            ],
        },
    )
async def _websocket_exchange(
    app: Any,
    incoming: Iterable[dict[str, Any]],
    *,
    authorization: bytes | None = b"Bearer valid-test-token",
    subprotocols: Iterable[str] = ("hermes.connector.v1",),
    fail_send_message_type: str | None = None,
) -> list[dict[str, Any]]:
    messages = iter(incoming)
    outgoing: list[dict[str, Any]] = []
    headers = [] if authorization is None else [(b"authorization", authorization)]

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        if (
            fail_send_message_type is not None
            and isinstance(message.get("text"), str)
            and json.loads(message["text"]).get("message_type")
            == fail_send_message_type
        ):
            raise ConnectionError("injected send failure")
        outgoing.append(message)

    await app(
        {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "wss",
            "path": "/api/ws",
            "raw_path": b"/api/ws",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8102),
            "subprotocols": list(subprotocols),
        },
        receive,
        send,
    )
    return outgoing


async def _get(app: Any, path: str) -> tuple[int, dict[str, Any]]:
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
        },
        receive,
        send,
    )
    start = next(
        message for message in outgoing if message["type"] == "http.response.start"
    )
    body = next(
        message for message in outgoing if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body["body"])


def test_authenticated_hello_negotiates_and_sends_welcome() -> None:
    async def scenario() -> None:
        authenticator = FakeAuthenticator()
        app = create_app(
            authenticator=authenticator,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert authenticator.tokens == ["valid-test-token"]
        assert outgoing[0] == {
            "type": "websocket.accept",
            "subprotocol": "hermes.connector.v1",
        }
        welcome = json.loads(outgoing[1]["text"])
        assert welcome["message_type"] == "connector.welcome"
        assert welcome["tenant_id"] == "tenant-test"
        assert welcome["device_id"] == "device-test"
        assert welcome["sequence"] == 0
        assert welcome["payload"]["accepted_capabilities"] == ["session.observe"]
        assert welcome["payload"]["unavailable_optional_capabilities"] == [
            "session.control"
        ]
        assert welcome["payload"]["resume_decision"] == "fresh"
        assert welcome["payload"]["next_connector_sequence"] == 1
        assert welcome["payload"]["next_cloud_sequence"] == 1

        await app.shutdown()

    asyncio.run(scenario())


def test_fresh_activation_conflict_prevents_router_registration_and_welcome() -> None:
    async def scenario() -> None:
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(
            events,
            activation_failure=RuntimeError("transport ownership changed"),
        )
        router = RecordingCommandRouter(events)
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
            command_router=router,
            available_capabilities=("session.control",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=("session.control",), optional=()),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert events == ["authority.prepare"]
        assert router.connected == []
        assert all(message["type"] != "websocket.send" for message in outgoing)
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": 1002,
            "reason": "protocol_violation",
        }
        await app.shutdown()

    asyncio.run(scenario())


def test_successful_handshake_confirms_after_welcome_before_router_registration() -> (
    None
):
    async def scenario() -> None:
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(events)
        router = RecordingCommandRouter(events)
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
            command_router=router,
            available_capabilities=("session.control",),
        )
        await app.startup()

        await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=("session.control",), optional=()),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert events[:3] == [
            "authority.prepare",
            "authority.confirm",
            "router.connected",
        ]
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "close_code"),
    (
        (RuntimeError("ownership changed"), 1002),
        (TimeoutError(), 1008),
    ),
)
def test_sent_welcome_keeps_activation_proof_when_confirmation_fails(
    failure: Exception,
    close_code: int,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(
            events,
            confirmation_failure=failure,
        )
        router = RecordingCommandRouter(events)
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
            command_router=router,
            available_capabilities=("session.control",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=("session.control",), optional=()),
                },
            ),
        )

        assert any(
            message["type"] == "websocket.send"
            and json.loads(message["text"])["message_type"] == "connector.welcome"
            for message in outgoing
        )
        assert events == ["authority.prepare", "authority.confirm"]
        assert router.connected == []
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == close_code
        await app.shutdown()

    asyncio.run(scenario())


def test_disconnect_cleanup_retries_once_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(
            events,
            disconnect_failures=1,
        )
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        with caplog.at_level(logging.WARNING):
            await _websocket_exchange(
                app,
                (
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": _hello(optional=())},
                    {"type": "websocket.disconnect", "code": 1000},
                ),
            )

        assert authority.disconnect_attempts == 2
        assert app._gateway_service.transport_cleanup_failure_count == 1
        assert app._gateway_service.transport_cleanup_reconcile_required_count == 0
        failure_records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "connector_transport_disconnect_failed"
        ]
        assert len(failure_records) == 1
        assert failure_records[0].attempt == 1
        assert failure_records[0].terminal is False
        assert "secret-frame-body-must-not-leak" not in caplog.text
        await app.shutdown()

    asyncio.run(scenario())


def test_permanent_disconnect_failure_is_observable_and_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(
            events,
            disconnect_failures=10,
        )
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        with caplog.at_level(logging.WARNING):
            await _websocket_exchange(
                app,
                (
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": _hello(optional=())},
                    {"type": "websocket.disconnect", "code": 1000},
                ),
            )

        assert authority.disconnect_attempts == 2
        assert app._gateway_service.transport_cleanup_failure_count == 2
        assert app._gateway_service.transport_cleanup_reconcile_required_count == 1
        failure_records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "connector_transport_disconnect_failed"
        ]
        assert [record.attempt for record in failure_records] == [1, 2]
        assert [record.terminal for record in failure_records] == [False, True]
        assert "secret-frame-body-must-not-leak" not in caplog.text
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_snapshot_and_event_commit_before_inbound_cursor_advances() -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello(optional=())},
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot",
                        sequence=1,
                        fixture="valid/session-snapshot-payload.json",
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.event",
                        sequence=2,
                        fixture="valid/session-event-payload.json",
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert len(ingress.snapshots) == 1
        assert len(ingress.events) == 1
        snapshot = ingress.snapshots[0]
        event = ingress.events[0]
        assert snapshot["runtime_generation"] == "runtime-test"
        assert snapshot["connector_instance_id"] == (
            "11111111-1111-4111-8111-111111111111"
        )
        assert snapshot["envelope"].sequence == 1
        assert snapshot["payload"].event_sequence == 5
        assert event["envelope"].sequence == 2
        assert event["payload"].event_sequence == 6
        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        acknowledgements = [
            frame for frame in frames if frame["message_type"] == "stream.ack"
        ]
        assert [frame["sequence"] for frame in acknowledgements] == [1, 2]
        assert [
            frame["payload"]["observer_message_type"] for frame in acknowledgements
        ] == ["session.snapshot", "session.event"]
        expected_digest = hashlib.sha256(
            json.dumps(
                event["envelope"].payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        assert acknowledgements[1]["payload"] == {
            "observer_message_id": event["envelope"].message_id,
            "payload_digest": expected_digest,
            "connector_sequence": 2,
            "observer_message_type": "session.event",
            "profile": "default",
            "session_key": "session-root-1",
            "runtime_generation": "runtime-test",
            "runtime_session_id": "runtime-session-1",
            "event_sequence": 6,
            "committed_at": acknowledgements[1]["payload"]["committed_at"],
        }
        assert all(message["type"] != "websocket.close" for message in outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_catalog_terminal_snapshot_commits_before_exact_ack_and_cursor_advance() -> None:
    async def scenario() -> None:
        ingress = FakeSessionCatalogIngress()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            session_catalog_ingress=ingress,
            available_capabilities=("session.catalog.v1",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(
                        required=("session.catalog.v1",), optional=()
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _catalog_snapshot_frame(sequence=1, is_last=True),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert len(ingress.pages) == 1
        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        assert frames[0]["payload"]["accepted_capabilities"] == [
            "session.catalog.v1"
        ]
        receipt = next(
            frame
            for frame in frames
            if frame["message_type"] == "session.catalog.ack"
        )
        page = ingress.pages[0]
        assert receipt["sequence"] == 1
        assert receipt["idempotency_key"] == page["envelope"].message_id
        assert receipt["payload"]["acked_message_id"] == (
            page["envelope"].message_id
        )
        assert receipt["payload"]["acked_connector_sequence"] == 1
        assert all(message["type"] != "websocket.close" for message in outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_gateway_rejects_non_atomic_catalog_ingress_at_composition() -> None:
    with pytest.raises(TypeError, match="atomic transport cursor"):
        create_app(
            authenticator=ObserverAuthenticator(),
            session_catalog_ingress=LegacyNonAtomicSessionCatalogIngress(),
            available_capabilities=("session.catalog.v1",),
        )


def test_catalog_receipt_send_failure_redelivers_after_reconnect() -> None:
    async def scenario() -> None:
        ingress = FakeSessionCatalogIngress()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            session_catalog_ingress=ingress,
            available_capabilities=("session.catalog.v1",),
        )
        await app.startup()

        first = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=("session.catalog.v1",), optional=()),
                },
                {
                    "type": "websocket.receive",
                    "text": _catalog_snapshot_frame(sequence=1, is_last=True),
                },
            ),
            fail_send_message_type="session.catalog.ack",
        )
        assert ingress.pending_receipt is not None
        assert ingress.sent == []
        assert all(
            json.loads(message["text"]).get("message_type")
            != "session.catalog.ack"
            for message in first
            if isinstance(message.get("text"), str)
        )

        second = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=("session.catalog.v1",), optional=()),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )
        acknowledgements = [
            json.loads(message["text"])
            for message in second
            if isinstance(message.get("text"), str)
            and json.loads(message["text"]).get("message_type")
            == "session.catalog.ack"
        ]
        assert len(acknowledgements) == 1
        assert acknowledgements[0]["sequence"] == 1
        assert len(ingress.pages) == 1
        assert len(ingress.reservations) >= 1
        assert len(ingress.sent) == 1
        assert ingress.settlements == []
        assert ingress.pending_receipt is not None
        await app.shutdown()

    asyncio.run(scenario())


def test_catalog_receipt_settles_only_after_same_connection_heartbeat_cursor_proof() -> (
    None
):
    async def scenario() -> None:
        ingress = FakeSessionCatalogIngress()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            session_catalog_ingress=ingress,
            available_capabilities=("session.catalog.v1",),
        )
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": _hello(required=("session.catalog.v1",), optional=()),
            }
        )
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": _catalog_snapshot_frame(sequence=1, is_last=True),
            }
        )

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        connection_id: str | None = None

        async def send(message: dict[str, Any]) -> None:
            nonlocal connection_id
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            envelope = json.loads(message["text"])
            if envelope["message_type"] == "connector.welcome":
                connection_id = envelope["payload"]["connection_id"]
            elif envelope["message_type"] == "session.catalog.ack":
                assert connection_id is not None
                await incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": _heartbeat(
                            connection_id=connection_id,
                            sequence=2,
                            next_outbound_sequence=2,
                            next_inbound_sequence=2,
                        ),
                    }
                )
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        assert len(ingress.sent) == 1
        assert len(ingress.settlements) == 1
        assert ingress.settlements[0]["connection_id"] == connection_id
        assert ingress.settlements[0]["durable_next_inbound_sequence"] == 2
        assert ingress.pending_receipt is None
        assert all(message["type"] != "websocket.close" for message in outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_v2_capability_selects_exact_ingress_and_ack_contract() -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        receipts = FakeObserverReceiptRouter()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            observer_receipt_router=receipts,
            available_capabilities=(
                "session.observe",
                "session.observe.output-parity.v1",
            ),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(
                        required=(
                            "session.observe",
                            "session.observe.output-parity.v1",
                        ),
                        optional=(),
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot.v2",
                        sequence=1,
                        fixture="valid/session-snapshot-v2-payload.json",
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert len(ingress.snapshots) == 1
        assert ingress.snapshots[0]["payload"].observer_contract == 2
        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        assert frames[0]["payload"]["accepted_capabilities"] == [
            "session.observe",
            "session.observe.output-parity.v1",
        ]
        receipt = next(
            frame for frame in frames if frame["message_type"] == "stream.ack.v2"
        )
        assert receipt["payload"]["observer_contract"] == 2
        assert receipt["payload"]["observer_message_type"] == "session.snapshot.v2"
        assert receipts.staged[0]["receipt_type"] == "stream.ack"
        assert receipts.staged[0]["payload"]["observer_contract"] == 2
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("message_type", "fixture", "v2_active"),
    (
        ("session.snapshot.v2", "valid/session-snapshot-payload.json", True),
        ("session.event.v2", "valid/session-event-payload.json", True),
        ("session.snapshot", "valid/session-snapshot-v2-payload.json", False),
        ("session.event", "valid/session-event-v2-tool-upsert.json", False),
    ),
)
def test_observer_message_type_and_payload_contract_must_match_before_ingress(
    message_type: str,
    fixture: str,
    v2_active: bool,
) -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        receipts = FakeObserverReceiptRouter()
        capabilities = (
            ("session.observe", "session.observe.output-parity.v1")
            if v2_active
            else ("session.observe",)
        )
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            observer_receipt_router=receipts,
            available_capabilities=capabilities,
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=capabilities, optional=()),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type=message_type,
                        sequence=1,
                        fixture=fixture,
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert ingress.snapshots == []
        assert ingress.events == []
        assert receipts.staged == []
        assert receipts.sent == []
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": 1002,
            "reason": "protocol_violation",
        }
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "credential",
    (
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "password=hunter2",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
    ),
)
def test_observer_v2_credential_display_text_fails_before_ingress_or_ack(
    credential: str,
) -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        receipts = FakeObserverReceiptRouter()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            observer_receipt_router=receipts,
            available_capabilities=(
                "session.observe",
                "session.observe.output-parity.v1",
            ),
        )
        await app.startup()
        unsafe_frame = json.loads(
            _observer_frame(
                message_type="session.snapshot.v2",
                sequence=1,
                fixture="valid/session-snapshot-v2-payload.json",
            )
        )
        unsafe_frame["payload"]["messages"] = [
            {
                "role": "assistant",
                "content": credential,
            }
        ]

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(
                        required=(
                            "session.observe",
                            "session.observe.output-parity.v1",
                        ),
                        optional=(),
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": json.dumps(unsafe_frame, separators=(",", ":")),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert ingress.snapshots == []
        assert ingress.events == []
        assert receipts.staged == []
        assert receipts.sent == []
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": 1002,
            "reason": "protocol_violation",
        }
        assert credential not in json.dumps(outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_v2_active_session_rejects_v1_midstream_change() -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            available_capabilities=(
                "session.observe",
                "session.observe.output-parity.v1",
            ),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(
                        required=(
                            "session.observe",
                            "session.observe.output-parity.v1",
                        ),
                        optional=(),
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot",
                        sequence=1,
                        fixture="valid/session-snapshot-payload.json",
                    ),
                },
            ),
        )

        assert ingress.snapshots == []
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": 1002,
            "reason": "protocol_violation",
        }
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_receipt_is_persisted_before_send_and_marked_before_cursor_advance() -> (
    None
):
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        receipts = FakeObserverReceiptRouter()
        authority = RecordingTransportCursorAuthority([])
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            observer_receipt_router=receipts,
            transport_cursor_authority=authority,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello(optional=())},
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot",
                        sequence=1,
                        fixture="valid/session-snapshot-payload.json",
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        ack = next(
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
            and json.loads(message["text"])["message_type"] == "stream.ack"
        )
        assert len(receipts.staged) == 1
        assert len(receipts.sent) == 1
        assert receipts.staged[0]["sequence"] == 1
        assert (
            receipts.staged[0]["observer_message_id"]
            == (ack["payload"]["observer_message_id"])
        )
        assert receipts.sent[0]["message_id"] == ack["message_id"]
        observer_commit = next(
            commit
            for commit in authority.commits
            if commit["next_connector_sequence"] == 2
            and commit["next_cloud_sequence"] == 2
        )
        assert observer_commit["expected_next_connector_sequence"] == 1
        assert observer_commit["expected_next_cloud_sequence"] == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_pending_observer_receipt_redelivers_at_current_cursor_and_same_connection_heartbeat_confirms() -> (
    None
):
    async def scenario() -> None:
        observer_message_id = "55555555-5555-4555-8555-555555555555"
        receipts = FakeObserverReceiptRouter(pending=(observer_message_id,))
        authority = RecordingTransportCursorAuthority([])
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_receipt_router=receipts,
            transport_cursor_authority=authority,
            available_capabilities=("session.observe",),
        )
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello(optional=())})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def enqueue_confirmation(connection_id: str) -> None:
            await authority.commit_recorded.wait()
            await incoming.put(
                {
                    "type": "websocket.receive",
                    "text": _heartbeat(
                        connection_id=connection_id,
                        sequence=1,
                        next_outbound_sequence=1,
                        next_inbound_sequence=2,
                    ),
                }
            )
            await incoming.put({"type": "websocket.disconnect", "code": 1000})

        connection_id: str | None = None

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal connection_id
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            envelope = json.loads(message["text"])
            if envelope["message_type"] == "connector.welcome":
                connection_id = envelope["payload"]["connection_id"]
            elif envelope["message_type"] == "stream.ack":
                assert connection_id is not None
                asyncio.create_task(enqueue_confirmation(connection_id))

        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            tracking_send,
        )

        redelivery = next(
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
            and json.loads(message["text"])["message_type"] == "stream.ack"
        )
        assert redelivery["sequence"] == 1
        assert redelivery["message_id"] == ("77777777-7777-4777-8777-777777777777")
        assert redelivery["idempotency_key"] == observer_message_id
        assert receipts.redeliveries[0]["sequence"] == 1
        assert len(receipts.confirmed) == 1
        assert receipts.confirmed[0]["connection_id"] == connection_id
        assert receipts.confirmed[0]["durable_next_inbound_sequence"] == 2
        assert authority.commits[0]["next_cloud_sequence"] == 2
        assert authority.commits[1]["next_connector_sequence"] == 2
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_gap_advances_transport_cursor_and_accepts_replacement_snapshot() -> (
    None
):
    async def scenario() -> None:
        ingress = FakeObserverIngress(
            event_failure=ConnectorObserverRejected(
                reason="event_gap",
                expected_event_sequence=6,
                recovery="send_snapshot",
            )
        )
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello(optional=())},
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot",
                        sequence=1,
                        fixture="valid/session-snapshot-payload.json",
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.event",
                        sequence=2,
                        fixture="valid/session-event-payload.json",
                        event_sequence=7,
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot",
                        sequence=3,
                        fixture="valid/session-snapshot-payload.json",
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        nack = next(frame for frame in frames if frame["message_type"] == "stream.nack")
        assert nack["sequence"] == 2
        assert nack["payload"]["connector_sequence"] == 2
        assert nack["payload"]["event_sequence"] == 7
        assert nack["payload"]["reason"] == "event_gap"
        assert nack["payload"]["expected_event_sequence"] == 6
        assert nack["payload"]["recovery"] == "send_snapshot"
        acknowledgements = [
            frame for frame in frames if frame["message_type"] == "stream.ack"
        ]
        assert [
            frame["payload"]["connector_sequence"] for frame in acknowledgements
        ] == [
            1,
            3,
        ]
        assert len(ingress.snapshots) == 2
        assert len(ingress.events) == 1
        assert all(message["type"] != "websocket.close" for message in outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_v2_projection_conflict_returns_exact_v2_nack() -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress(
            event_failure=ConnectorObserverRejected(
                reason="projection_conflict",
                expected_event_sequence=5,
                recovery="send_snapshot",
            )
        )
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            available_capabilities=(
                "session.observe",
                "session.observe.output-parity.v1",
            ),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(
                        required=(
                            "session.observe",
                            "session.observe.output-parity.v1",
                        ),
                        optional=(),
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot.v2",
                        sequence=1,
                        fixture="valid/session-snapshot-v2-payload.json",
                    ),
                },
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.event.v2",
                        sequence=2,
                        fixture="valid/session-event-v2-tool-upsert.json",
                        event_sequence=5,
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        nack = next(
            frame for frame in frames if frame["message_type"] == "stream.nack.v2"
        )
        assert nack["payload"]["observer_contract"] == 2
        assert nack["payload"]["observer_message_type"] == "session.event.v2"
        assert nack["payload"]["reason"] == "projection_conflict"
        assert nack["payload"]["expected_event_sequence"] == 5
        assert nack["payload"]["recovery"] == "send_snapshot"
        await app.shutdown()

    asyncio.run(scenario())


def test_failed_observer_commit_closes_without_accepting_later_sequence() -> None:
    async def scenario() -> None:
        ingress = FakeObserverIngress(failure=RuntimeError("commit failed"))
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello(optional=())},
                {
                    "type": "websocket.receive",
                    "text": _observer_frame(
                        message_type="session.snapshot",
                        sequence=1,
                        fixture="valid/session-snapshot-payload.json",
                    ),
                },
            ),
        )

        assert len(ingress.snapshots) == 1
        assert ingress.events == []
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


def test_failed_observer_receipt_stage_does_not_send_or_advance_transport_cursor() -> (
    None
):
    async def scenario() -> None:
        ingress = FakeObserverIngress()
        receipts = FailingStageObserverReceiptRouter()
        authority = RecordingTransportCursorAuthority([])
        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_ingress=ingress,
            observer_receipt_router=receipts,
            transport_cursor_authority=authority,
            available_capabilities=("session.observe",),
        )
        await app.startup()

        with pytest.raises(RuntimeError, match="receipt outbox unavailable"):
            await _websocket_exchange(
                app,
                (
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": _hello(optional=())},
                    {
                        "type": "websocket.receive",
                        "text": _observer_frame(
                            message_type="session.snapshot",
                            sequence=1,
                            fixture="valid/session-snapshot-payload.json",
                        ),
                    },
                ),
            )

        assert len(ingress.snapshots) == 1
        assert len(receipts.staged) == 1
        assert receipts.sent == []
        assert authority.commits == []
        await app.shutdown()

    asyncio.run(scenario())


def test_injected_frame_decoder_is_authoritative_for_live_websocket() -> None:
    async def scenario() -> None:
        frame_decoder = RejectingFrameDecoder()
        app = create_app(
            authenticator=FakeAuthenticator(),
            frame_decoder=frame_decoder,
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
            ),
        )

        assert frame_decoder.frames == [_hello()]
        assert all(message["type"] != "websocket.send" for message in outgoing)
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "subprotocols",
    (
        (),
        ("another.protocol",),
        ("hermes.connector.v1", "another.protocol"),
        ("hermes.connector.v1", "hermes.connector.v1"),
    ),
)
def test_connector_subprotocol_must_be_uniquely_proposed_before_authentication(
    subprotocols: tuple[str, ...],
) -> None:
    async def scenario() -> None:
        authenticator = FakeAuthenticator()
        app = create_app(authenticator=authenticator)
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.disconnect", "code": 1000},
            ),
            subprotocols=subprotocols,
        )

        assert authenticator.tokens == []
        assert outgoing == [
            {
                "type": "websocket.close",
                "code": 1002,
                "reason": "subprotocol_required",
            }
        ]
        await app.shutdown()

    asyncio.run(scenario())


def test_default_authenticator_blocks_readiness_and_websocket() -> None:
    async def scenario() -> None:
        app = create_app()
        await app.startup()

        ready_status, snapshot = await _get(app, "/ready")
        outgoing = await _websocket_exchange(
            app,
            ({"type": "websocket.connect"},),
        )

        assert ready_status == 503
        assert snapshot["ready"] is False
        assert snapshot["state"] == "READY"
        assert snapshot["diagnostic"] == "BLOCKED"
        assert outgoing == [
            {
                "type": "websocket.close",
                "code": 1013,
                "reason": "gateway_not_ready",
            }
        ]
        await app.shutdown()

    asyncio.run(scenario())


def test_missing_bearer_fails_closed_before_accept_or_authentication() -> None:
    async def scenario() -> None:
        authenticator = FakeAuthenticator()
        app = create_app(authenticator=authenticator)
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            ({"type": "websocket.connect"},),
            authorization=None,
        )

        assert authenticator.tokens == []
        assert outgoing == [
            {
                "type": "websocket.close",
                "code": 1008,
                "reason": "authentication_required",
            }
        ]
        await app.shutdown()

    asyncio.run(scenario())


def test_authenticated_identity_must_match_hello_envelope() -> None:
    async def scenario() -> None:
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _replace_envelope(
                        _hello(),
                        tenant_id="another-tenant",
                    ),
                },
            ),
        )

        assert outgoing[0] == {
            "type": "websocket.accept",
            "subprotocol": "hermes.connector.v1",
        }
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1008
        await app.shutdown()

    asyncio.run(scenario())


def test_first_frame_must_be_connector_hello() -> None:
    async def scenario() -> None:
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _heartbeat(
                        connection_id=("44444444-4444-4444-8444-444444444444"),
                        sequence=0,
                        next_outbound_sequence=0,
                        next_inbound_sequence=0,
                    ),
                },
            ),
        )

        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


def test_hello_envelope_sequence_must_match_proposed_outbound_cursor() -> None:
    async def scenario() -> None:
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _replace_envelope(_hello(), sequence=3),
                },
            ),
        )

        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


def test_binary_frame_is_rejected_as_unsupported_data() -> None:
    async def scenario() -> None:
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "bytes": _hello().encode()},
            ),
        )

        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1003
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    (
        _hello() + "\n{}",
        _hello().replace(
            '"runtime_generation":"runtime-test"',
            ('"runtime_generation":"runtime-test","runtime_generation":"duplicate"'),
        ),
        _hello(
            required=("session.observe",),
            optional=("session.observe",),
        ),
    ),
)
def test_hello_requires_one_strict_authoritative_json_document(raw: str) -> None:
    async def scenario() -> None:
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": raw},
            ),
        )

        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


def test_oversized_text_frame_closes_with_message_too_big() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["payload"]["extensions"] = {
            "com.example.padding": {"value": "x" * 262_144}
        }
        raw = json.dumps(hello, separators=(",", ":"))
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": raw},
            ),
        )

        assert len(raw.encode("utf-8")) > 262_144
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1009
        await app.shutdown()

    asyncio.run(scenario())


def test_missing_required_capability_fails_before_session_effect() -> None:
    async def scenario() -> None:
        app = create_app(
            authenticator=FakeAuthenticator(),
            available_capabilities=("session.observe",),
        )
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(required=("enterprise.data",)),
                },
            ),
        )

        assert all(message["type"] != "websocket.send" for message in outgoing)
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1008
        await app.shutdown()

    asyncio.run(scenario())


def test_resume_without_injected_authority_starts_an_explicit_new_epoch() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "resume",
            "previous_connection_id": ("44444444-4444-4444-8444-444444444444"),
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(hello, separators=(",", ":")),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        welcome = json.loads(outgoing[1]["text"])
        assert welcome["payload"]["resume_decision"] == "fresh"
        assert welcome["payload"]["next_connector_sequence"] == 0
        assert welcome["payload"]["next_cloud_sequence"] == 0
        await app.shutdown()

    asyncio.run(scenario())


def test_nonzero_fresh_proposal_resets_to_a_preserved_new_epoch() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "fresh",
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(hello, separators=(",", ":")),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        welcome = json.loads(outgoing[1]["text"])
        assert welcome["payload"]["resume_decision"] == "fresh"
        assert welcome["payload"]["next_connector_sequence"] == 0
        assert welcome["payload"]["next_cloud_sequence"] == 0
        await app.shutdown()

    asyncio.run(scenario())


def test_reset_required_session_stays_reconciling_without_completion_signal() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "resume",
            "previous_connection_id": ("44444444-4444-4444-8444-444444444444"),
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        sleeper = ControlledSleep()
        resolver = RecordingTransportCursorAuthority(
            [],
            resume_decision="reset_required",
            handshake_disposition="preserve",
        )
        app = create_app(
            authenticator=FakeAuthenticator(),
            resume_resolver=resolver,
            settings=ConnectorGatewaySettings(
                heartbeat_interval_ms=5_000,
                heartbeat_timeout_seconds=1.0,
            ),
            sleep=sleeper,
        )
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        welcome_sent = asyncio.Event()
        heartbeat_sent = asyncio.Event()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(hello, separators=(",", ":")),
            }
        )

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            decoded = json.loads(message["text"])
            if decoded["message_type"] == "connector.welcome":
                welcome_sent.set()
            if decoded["message_type"] == "connector.heartbeat":
                heartbeat_sent.set()
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        )
        await welcome_sent.wait()
        await sleeper.called.wait()
        sleeper.release.set()
        await asyncio.wait_for(heartbeat_sent.wait(), timeout=0.1)
        await asyncio.wait_for(task, timeout=0.1)

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        welcome, heartbeat = frames
        assert welcome["payload"]["resume_decision"] == "reset_required"
        assert heartbeat["payload"]["session_state"] == "reconciling"
        await app.shutdown()

    asyncio.run(scenario())


def test_injected_resume_authority_can_confirm_safe_resume() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "resume",
            "previous_connection_id": ("44444444-4444-4444-8444-444444444444"),
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        resolver = FakeResumeResolver()
        app = create_app(
            authenticator=FakeAuthenticator(),
            resume_resolver=resolver,
        )
        await app.startup()
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(hello, separators=(",", ":")),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        welcome = json.loads(outgoing[1]["text"])
        assert welcome["sequence"] == 11
        assert welcome["payload"]["resume_decision"] == "resumed"
        assert welcome["payload"]["next_connector_sequence"] == 8
        assert welcome["payload"]["next_cloud_sequence"] == 12
        assert len(resolver.calls) == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_resume_from_an_old_epoch_activates_an_explicit_fresh_epoch() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "resume",
            "previous_connection_id": ("44444444-4444-4444-8444-444444444444"),
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(
            events,
            resume_decision="fresh",
            handshake_disposition="preserve",
        )
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(hello, separators=(",", ":")),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        welcome = json.loads(outgoing[1]["text"])
        assert welcome["payload"]["resume_decision"] == "fresh"
        assert welcome["payload"]["next_connector_sequence"] == 0
        assert welcome["payload"]["next_cloud_sequence"] == 0
        assert len(authority.preparations) == 1
        activation = authority.preparations[0]
        assert activation["resume_decision"] == "fresh"
        assert activation["handshake_disposition"] == "preserve"
        assert activation["previous_connection_id"] is None
        assert activation["expected_next_connector_sequence"] == 0
        assert activation["expected_next_cloud_sequence"] == 0
        assert activation["next_connector_sequence"] == 0
        assert activation["next_cloud_sequence"] == 0
        await app.shutdown()

    asyncio.run(scenario())


def test_rollover_first_frames_start_at_zero_and_converge_both_cursors() -> None:
    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "resume",
            "previous_connection_id": ("44444444-4444-4444-8444-444444444444"),
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        events: list[str] = []
        authority = RecordingTransportCursorAuthority(
            events,
            resume_decision="fresh",
            handshake_disposition="preserve",
        )
        sleeper = ControlledSleep()
        app = create_app(
            authenticator=FakeAuthenticator(),
            transport_cursor_authority=authority,
            settings=ConnectorGatewaySettings(
                heartbeat_interval_ms=5_000,
                heartbeat_timeout_seconds=1.0,
            ),
            sleep=sleeper,
        )
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        heartbeat_sent = asyncio.Event()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(hello, separators=(",", ":")),
            }
        )

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            decoded = json.loads(message["text"])
            if decoded["message_type"] == "connector.welcome":
                await incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": _heartbeat(
                            connection_id=decoded["payload"]["connection_id"],
                            sequence=0,
                            next_outbound_sequence=0,
                            next_inbound_sequence=0,
                        ),
                    }
                )
            elif decoded["message_type"] == "connector.heartbeat":
                heartbeat_sent.set()
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        )
        await asyncio.wait_for(authority.commit_recorded.wait(), timeout=0.1)
        sleeper.release.set()
        await asyncio.wait_for(heartbeat_sent.wait(), timeout=0.1)
        await asyncio.wait_for(task, timeout=0.1)

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        welcome, cloud_heartbeat = frames
        assert welcome["payload"]["resume_decision"] == "fresh"
        assert cloud_heartbeat["sequence"] == 0
        assert cloud_heartbeat["payload"]["next_outbound_sequence"] == 0
        assert cloud_heartbeat["payload"]["next_inbound_sequence"] == 1
        assert [
            (
                commit["expected_next_connector_sequence"],
                commit["expected_next_cloud_sequence"],
                commit["next_connector_sequence"],
                commit["next_cloud_sequence"],
            )
            for commit in authority.commits
        ] == [(0, 0, 1, 0), (1, 0, 1, 1)]
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "resolution",
    (
        object(),
        SimpleNamespace(
            decision="resumed",
            next_connector_sequence="7",
            next_cloud_sequence=11,
        ),
        SimpleNamespace(
            decision="resumed",
            next_connector_sequence=True,
            next_cloud_sequence=11,
        ),
        SimpleNamespace(
            decision="resumed",
            next_connector_sequence=-1,
            next_cloud_sequence=11,
        ),
        SimpleNamespace(
            decision="resumed",
            next_connector_sequence=7,
            next_cloud_sequence=11,
            handshake_disposition="preserve",
        ),
        SimpleNamespace(
            decision="reset_required",
            next_connector_sequence=7,
            next_cloud_sequence=11,
            handshake_disposition="advance",
        ),
        SimpleNamespace(
            decision="fresh",
            next_connector_sequence=0,
            next_cloud_sequence=0,
            handshake_disposition="advance",
        ),
    ),
)
def test_malformed_resume_authority_result_fails_closed_before_welcome(
    resolution: object,
) -> None:
    class MalformedResumeResolver:
        async def resolve(
            self,
            identity: object,
            position: object,
            **_binding: object,
        ):
            return resolution

    async def scenario() -> None:
        hello = json.loads(_hello())
        hello["sequence"] = 7
        hello["payload"]["resume"] = {
            "mode": "resume",
            "previous_connection_id": ("44444444-4444-4444-8444-444444444444"),
            "next_outbound_sequence": 7,
            "next_inbound_sequence": 11,
        }
        app = create_app(
            authenticator=FakeAuthenticator(),
            resume_resolver=MalformedResumeResolver(),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": json.dumps(hello, separators=(",", ":")),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert all(message["type"] != "websocket.send" for message in outgoing)
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": 1002,
            "reason": "protocol_violation",
        }
        await app.shutdown()

    asyncio.run(scenario())


def test_malformed_resume_authority_cannot_silently_desync_fresh_handshake() -> None:
    class MalformedResumeResolver:
        async def resolve(self, *_args: object, **_binding: object):
            return SimpleNamespace(
                decision="fresh",
                next_connector_sequence=0,
                next_cloud_sequence=0,
                handshake_disposition="preserve",
            )

    async def scenario() -> None:
        app = create_app(
            authenticator=FakeAuthenticator(),
            resume_resolver=MalformedResumeResolver(),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
                {"type": "websocket.disconnect", "code": 1000},
            ),
        )

        assert all(message["type"] != "websocket.send" for message in outgoing)
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": 1002,
            "reason": "protocol_violation",
        }
        await app.shutdown()

    asyncio.run(scenario())


def test_valid_connector_heartbeat_cursors_are_accepted() -> None:
    async def scenario() -> None:
        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        for message in (
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": _hello()},
        ):
            await incoming.put(message)

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] == "websocket.send":
                welcome = json.loads(message["text"])
                if welcome["message_type"] == "connector.welcome":
                    await incoming.put(
                        {
                            "type": "websocket.receive",
                            "text": _heartbeat(
                                connection_id=welcome["payload"]["connection_id"],
                                sequence=1,
                                next_outbound_sequence=1,
                                next_inbound_sequence=1,
                            ),
                        }
                    )
                    await incoming.put({"type": "websocket.disconnect", "code": 1000})

        scope = {
            "type": "websocket",
            "path": "/api/ws",
            "headers": [(b"authorization", b"Bearer valid-test-token")],
            "subprotocols": ["hermes.connector.v1"],
        }
        await app(scope, receive, send)

        assert not any(message["type"] == "websocket.close" for message in outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_invalid_connector_heartbeat_cursor_closes_protocol_error() -> None:
    async def scenario() -> None:
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            welcome = json.loads(message["text"])
            if welcome["message_type"] == "connector.welcome":
                await incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": _heartbeat(
                            connection_id=welcome["payload"]["connection_id"],
                            sequence=1,
                            next_outbound_sequence=1,
                            next_inbound_sequence=99,
                        ),
                    }
                )

        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "message_type",
    (
        "command.deliver",
        "command.receipt",
        "command.result",
        "file.transfer",
        "a2a.message",
        "view.card.invalidate",
    ),
)
def test_reserved_message_after_welcome_has_zero_effect_and_closes_1003(
    message_type: str,
) -> None:
    async def scenario() -> None:
        authenticator = FakeAuthenticator()
        app = create_app(authenticator=authenticator)
        await app.startup()
        reserved = _replace_envelope(
            _hello(),
            message_type=message_type,
            sequence=1,
            payload={},
        )
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
                {"type": "websocket.receive", "text": reserved},
            ),
        )

        assert authenticator.tokens == ["valid-test-token"]
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1003
        await app.shutdown()

    asyncio.run(scenario())


def test_command_router_delivers_and_persists_receipt_and_result() -> None:
    async def scenario() -> None:
        router = FakeCommandRouter()
        await router.deliveries.put(
            SimpleNamespace(
                command_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                sent_at="2026-07-31T00:00:00.000Z",
                payload={
                    "command_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "connector_instance_id": ("11111111-1111-4111-8111-111111111111"),
                    "client_instance_id": ("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                    "session_key": "durable-root-1",
                    "profile": "default",
                    "client_request_id": "req-client-01",
                    "method": "prompt.submit",
                    "params": {
                        "runtime_session_id": "runtime-7",
                        "runtime_generation": "runtime-test",
                        "client_turn_id": "turn-client-01",
                        "text": "Continue the current task.",
                    },
                    "issued_at": "2026-07-31T00:00:00Z",
                    "expires_at": "2026-07-31T00:05:00Z",
                    "revision": 1,
                },
            )
        )
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        def connector_response(
            delivered: dict[str, object],
            *,
            message_type: str,
            sequence: int,
            payload: dict[str, object],
        ) -> str:
            return _replace_envelope(
                _hello(),
                message_type=message_type,
                sequence=sequence,
                payload=payload,
            )

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            delivered = json.loads(message["text"])
            if delivered["message_type"] != "command.deliver":
                return
            command = delivered["payload"]
            common = {
                "command_id": command["command_id"],
                "connector_instance_id": command["connector_instance_id"],
                "client_instance_id": command["client_instance_id"],
                "session_key": command["session_key"],
                "profile": command["profile"],
                "client_request_id": command["client_request_id"],
                "method": command["method"],
            }
            await incoming.put(
                {
                    "type": "websocket.receive",
                    "text": connector_response(
                        delivered,
                        message_type="command.receipt",
                        sequence=1,
                        payload={
                            **common,
                            "message_id": delivered["message_id"],
                            "state": "delivered",
                            "stored_at": "2026-07-31T00:00:01Z",
                            "revision": 1,
                        },
                    ),
                }
            )
            await incoming.put(
                {
                    "type": "websocket.receive",
                    "text": connector_response(
                        delivered,
                        message_type="command.result",
                        sequence=2,
                        payload={
                            **common,
                            "state": "succeeded",
                            "completed_at": "2026-07-31T00:00:02Z",
                            "revision": 2,
                            "result": {
                                "status": "accepted",
                                "client_request_id": "req-client-01",
                                "client_turn_id": "turn-client-01",
                                "server_turn_id": "turn-server-09",
                            },
                        },
                    ),
                }
            )
            await incoming.put({"type": "websocket.disconnect", "code": 1000})

        app = create_app(
            authenticator=FakeAuthenticator(),
            command_router=router,
            available_capabilities=("session.observe", "session.control"),
        )
        await app.startup()
        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        assert [frame["message_type"] for frame in frames] == [
            "connector.welcome",
            "command.deliver",
        ]
        assert frames[1]["sequence"] == 1
        assert router.dispatched == [
            (
                "tenant-test",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
        ]
        assert [response.message_type for response in router.responses] == [
            "command.receipt",
            "command.result",
        ]
        assert len(router.connected) == 1
        assert len(router.disconnected) == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_subscription_router_delivers_fixed_authorized_open() -> None:
    async def scenario() -> None:
        router = FakeObserverSubscriptionRouter()
        payload = {
            "request_id": "81000000-0000-4000-8000-000000000001",
            "subscription_id": "82000000-0000-4000-8000-000000000001",
            "profile": "default",
            "session_key": "session-root-1",
            "target_source": "cloud_authorized_binding",
            "requested_at": "2026-07-31T09:00:00Z",
        }
        await router.deliveries.put(
            SimpleNamespace(
                request_id=payload["request_id"],
                message_id=payload["request_id"],
                message_type="session.observe.open",
                sent_at=payload["requested_at"],
                payload=payload,
            )
        )
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello(optional=())})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            envelope = json.loads(message["text"])
            if envelope["message_type"] == "session.observe.open":
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_subscription_router=router,
        )
        await app.startup()
        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        assert [frame["message_type"] for frame in frames] == [
            "connector.welcome",
            "session.observe.open",
        ]
        intent = frames[1]
        assert intent["message_id"] == payload["request_id"]
        assert intent["idempotency_key"] == payload["request_id"]
        assert intent["sequence"] == 1
        assert intent["payload"] == payload
        assert len(router.connected) == 1
        assert len(router.disconnected) == 1
        assert router.reserved[0]["sequence"] == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_observer_v2_subscription_converts_durable_intent_to_exact_v2_frame() -> None:
    async def scenario() -> None:
        router = FakeObserverSubscriptionRouter()
        payload = {
            "request_id": "81000000-0000-4000-8000-000000000201",
            "subscription_id": "82000000-0000-4000-8000-000000000201",
            "profile": "default",
            "session_key": "session-root-1",
            "target_source": "cloud_authorized_binding",
            "requested_at": "2026-07-31T09:00:00Z",
        }
        await router.deliveries.put(
            SimpleNamespace(
                request_id=payload["request_id"],
                message_id=payload["request_id"],
                message_type="session.observe.open",
                sent_at=payload["requested_at"],
                payload=payload,
            )
        )
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": _hello(
                    required=(
                        "session.observe",
                        "session.observe.output-parity.v1",
                    ),
                    optional=(),
                ),
            }
        )

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            envelope = json.loads(message["text"])
            if envelope["message_type"] == "session.observe.open.v2":
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        app = create_app(
            authenticator=ObserverAuthenticator(),
            observer_subscription_router=router,
            available_capabilities=(
                "session.observe",
                "session.observe.output-parity.v1",
            ),
        )
        await app.startup()
        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        intent = frames[1]
        assert intent["message_type"] == "session.observe.open.v2"
        assert intent["payload"] == {**payload, "observer_contract": 2}
        assert router.reserved[0]["sequence"] == 1
        await app.shutdown()

    asyncio.run(scenario())


def test_reset_replays_from_the_server_authoritative_cursor_pair() -> None:
    async def scenario() -> None:
        router = FakeObserverSubscriptionRouter()
        payload = {
            "request_id": "81000000-0000-4000-8000-000000000011",
            "subscription_id": "82000000-0000-4000-8000-000000000011",
            "profile": "default",
            "session_key": "session-reset-1",
            "target_source": "cloud_authorized_binding",
            "requested_at": "2026-07-31T09:00:00Z",
        }
        await router.deliveries.put(
            SimpleNamespace(
                request_id=payload["request_id"],
                message_id=payload["request_id"],
                message_type="session.observe.open",
                sent_at=payload["requested_at"],
                payload=payload,
            )
        )
        authority = RecordingTransportCursorAuthority(
            [],
            resume_decision="reset_required",
            next_connector_sequence=7,
            next_cloud_sequence=11,
            handshake_disposition="preserve",
        )
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello(optional=())})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            envelope = json.loads(message["text"])
            if envelope["message_type"] == "session.observe.open":
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        app = create_app(
            authenticator=ObserverAuthenticator(),
            transport_cursor_authority=authority,
            observer_subscription_router=router,
        )
        await app.startup()
        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        assert frames[0]["payload"]["resume_decision"] == "reset_required"
        assert frames[0]["payload"]["next_connector_sequence"] == 7
        assert frames[0]["payload"]["next_cloud_sequence"] == 11
        assert frames[1]["message_type"] == "session.observe.open"
        assert frames[1]["sequence"] == 11
        assert router.reserved[0]["sequence"] == 11
        await app.shutdown()

    asyncio.run(scenario())


def test_owner_control_router_round_trips_without_command_persistence() -> None:
    async def scenario() -> None:
        router = GatewayOwnerControlRouter()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        control_request_sent = asyncio.Event()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            envelope = json.loads(message["text"])
            if envelope["message_type"] != "control.request":
                return
            request = envelope["payload"]
            await incoming.put(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "contract_version": 1,
                            "message_id": ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                            "message_type": "control.response",
                            "tenant_id": "tenant-test",
                            "device_id": "device-test",
                            "sequence": 1,
                            "sent_at": "2026-07-31T00:00:01Z",
                            "idempotency_key": request["request_id"],
                            "payload": {
                                "request_id": request["request_id"],
                                "control_transport_id": (
                                    request["control_transport_id"]
                                ),
                                "operation": request["operation"],
                                "state": "succeeded",
                                "completed_at": "2026-07-31T00:00:01Z",
                                "result": {
                                    "attached": True,
                                    "connection_role": "control",
                                },
                            },
                        },
                        separators=(",", ":"),
                    ),
                }
            )
            control_request_sent.set()

        app = create_app(
            authenticator=FakeAuthenticator(),
            owner_control_router=router,
            available_capabilities=("session.observe", "session.control"),
        )
        await app.startup()
        session = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        )
        while not any(
            message["type"] == "websocket.send"
            and json.loads(message["text"])["message_type"] == "connector.welcome"
            for message in outgoing
        ):
            await asyncio.sleep(0)

        request = {
            "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "control_transport_id": ("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            "operation": "control.transport.open",
            "issued_at": "2026-07-31T00:00:00Z",
            "expires_at": "2099-07-31T00:00:03Z",
            "body": {
                "principal_id": "principal-1",
                "client_instance_id": ("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                "session_key": "session-root-1",
                "profile": "default",
            },
        }
        response = await asyncio.wait_for(
            router.handle_bridge_request(
                peer_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                route=ControlConnectorRoute(
                    "tenant-test",
                    "device-test",
                ),
                payload=request,
            ),
            timeout=1,
        )
        assert response["state"] == "succeeded"
        await control_request_sent.wait()
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await session
        await app.shutdown()

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        assert [frame["message_type"] for frame in frames] == [
            "connector.welcome",
            "control.request",
        ]
        assert frames[1]["idempotency_key"] == request["request_id"]
        assert frames[1]["payload"] == request

    asyncio.run(scenario())


def test_unknown_message_has_zero_effect_and_closes_protocol_error() -> None:
    async def scenario() -> None:
        authenticator = FakeAuthenticator()
        app = create_app(authenticator=authenticator)
        await app.startup()
        unknown = _replace_envelope(
            _hello(),
            message_type="server.private-command",
            sequence=1,
            payload={},
        )
        outgoing = await _websocket_exchange(
            app,
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
                {"type": "websocket.receive", "text": unknown},
            ),
        )

        assert authenticator.tokens == ["valid-test-token"]
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1002
        await app.shutdown()

    asyncio.run(scenario())


def test_duplicate_authorization_header_fails_before_authenticator() -> None:
    async def scenario() -> None:
        authenticator = FakeAuthenticator()
        app = create_app(authenticator=authenticator)
        await app.startup()
        incoming = iter(({"type": "websocket.connect"},))
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [
                    (b"authorization", b"Bearer first-token"),
                    (b"Authorization", b"Bearer second-token"),
                ],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        assert authenticator.tokens == []
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1008
        assert "first-token" not in repr(outgoing)
        assert "second-token" not in repr(outgoing)
        await app.shutdown()

    asyncio.run(scenario())


def test_server_sends_independent_heartbeat_with_bidirectional_cursors() -> None:
    async def scenario() -> None:
        sleeper = ControlledSleep()
        app = create_app(
            authenticator=FakeAuthenticator(),
            settings=ConnectorGatewaySettings(
                heartbeat_interval_ms=5_000,
                heartbeat_timeout_seconds=1.0,
            ),
            sleep=sleeper,
        )
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        welcome_sent = asyncio.Event()
        heartbeat_sent = asyncio.Event()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] != "websocket.send":
                return
            decoded = json.loads(message["text"])
            if decoded["message_type"] == "connector.welcome":
                welcome_sent.set()
            if decoded["message_type"] == "connector.heartbeat":
                heartbeat_sent.set()
                await incoming.put({"type": "websocket.disconnect", "code": 1000})

        task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        )
        await welcome_sent.wait()
        await sleeper.called.wait()
        sleeper.release.set()
        await asyncio.wait_for(heartbeat_sent.wait(), timeout=0.1)
        await asyncio.wait_for(task, timeout=0.1)

        frames = [
            json.loads(message["text"])
            for message in outgoing
            if message["type"] == "websocket.send"
        ]
        welcome, heartbeat = frames
        assert sleeper.delays
        assert set(sleeper.delays) == {5.0}
        assert heartbeat["message_type"] == "connector.heartbeat"
        assert heartbeat["sequence"] == 1
        assert heartbeat["payload"] == {
            "connection_id": welcome["payload"]["connection_id"],
            "sender_role": "cloud",
            "observed_at": heartbeat["payload"]["observed_at"],
            "next_outbound_sequence": 1,
            "next_inbound_sequence": 1,
            "session_state": "active",
        }
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "close_code", "close_reason"),
    (
        (
            ConnectorAuthorizationRevoked(),
            1008,
            "device_authorization_revoked",
        ),
        (
            ConnectorAuthorizationSuspended(),
            1008,
            "device_authorization_suspended",
        ),
        (
            PermissionError("authorization database unavailable"),
            1011,
            "authorization_recheck_unavailable",
        ),
        (
            ConnectorAuthenticationExpired("connector token expired"),
            1008,
            "authentication_expired",
        ),
    ),
)
def test_server_heartbeat_revalidation_preserves_authoritative_reason(
    error: Exception,
    close_code: int,
    close_reason: str,
) -> None:
    async def scenario() -> None:
        sleeper = ControlledSleep()
        authenticator = LifecycleOnHeartbeatAuthenticator(error)
        app = create_app(
            authenticator=authenticator,
            settings=ConnectorGatewaySettings(
                heartbeat_interval_ms=5_000,
                heartbeat_timeout_seconds=1.0,
            ),
            sleep=sleeper,
        )
        await app.startup()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        welcome_sent = asyncio.Event()
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if (
                message["type"] == "websocket.send"
                and json.loads(message["text"])["message_type"] == "connector.welcome"
            ):
                welcome_sent.set()

        task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        )
        await welcome_sent.wait()
        await sleeper.called.wait()
        sleeper.release.set()
        await asyncio.wait_for(task, timeout=0.1)

        assert authenticator.revalidations == 1
        assert outgoing[-1] == {
            "type": "websocket.close",
            "code": close_code,
            "reason": close_reason,
        }
        await app.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("block_second_router", (False, True))
def test_second_router_registration_failure_compensates_first_router(
    block_second_router: bool,
) -> None:
    async def scenario() -> None:
        command_router = FakeCommandRouter()
        owner_router = FailingSecondRouter(block=block_second_router)
        incoming = iter(
            (
                {"type": "websocket.connect"},
                {
                    "type": "websocket.receive",
                    "text": _hello(
                        required=("session.control",),
                        optional=(),
                    ),
                },
            )
        )
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        app = create_app(
            authenticator=FakeAuthenticator(),
            command_router=command_router,
            owner_control_router=owner_router,
            settings=ConnectorGatewaySettings(
                available_capabilities=("session.observe", "session.control"),
                router_timeout_seconds=0.01,
            ),
        )
        await app.startup()
        if block_second_router:
            await app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        else:
            with pytest.raises(
                RuntimeError,
                match="second router registration failed",
            ):
                await app(
                    {
                        "type": "websocket",
                        "path": "/api/ws",
                        "headers": [(b"authorization", b"Bearer valid-test-token")],
                        "subprotocols": ["hermes.connector.v1"],
                    },
                    receive,
                    send,
                )

        assert len(command_router.connected) == 1
        assert len(command_router.disconnected) == 1
        assert len(owner_router.connected) == 1
        assert owner_router.disconnected == []
        sent = [message for message in outgoing if message["type"] == "websocket.send"]
        assert len(sent) == 1
        assert json.loads(sent[0]["text"])["message_type"] == "connector.welcome"
        await app.shutdown()

    asyncio.run(scenario())


def test_authentication_deadline_cancels_provider_before_accept() -> None:
    async def scenario() -> None:
        authenticator = BlockingAuthenticator()
        app = create_app(
            authenticator=authenticator,
            settings=ConnectorGatewaySettings(negotiation_timeout_seconds=0.01),
        )
        await app.startup()

        outgoing = await _websocket_exchange(
            app,
            ({"type": "websocket.connect"},),
        )

        assert authenticator.started.is_set()
        assert authenticator.cancelled.is_set()
        assert all(message["type"] != "websocket.accept" for message in outgoing)
        assert outgoing[-1]["code"] == 1008
        await app.shutdown()

    asyncio.run(scenario())


def test_heartbeat_receive_deadline_cleans_blocked_receive() -> None:
    async def scenario() -> None:
        receive_cancelled = asyncio.Event()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        async def receive() -> dict[str, Any]:
            try:
                return await incoming.get()
            except asyncio.CancelledError:
                receive_cancelled.set()
                raise

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        app = create_app(
            authenticator=FakeAuthenticator(),
            settings=ConnectorGatewaySettings(
                heartbeat_interval_ms=120_000,
                heartbeat_timeout_seconds=0.01,
                transport_ownership_lease_seconds=131.0,
            ),
        )
        await app.startup()
        await asyncio.wait_for(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            ),
            timeout=0.1,
        )

        assert receive_cancelled.is_set()
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1001
        await app.shutdown()

    asyncio.run(scenario())


def test_active_session_cancellation_propagates_and_cleans_receive() -> None:
    async def scenario() -> None:
        receive_cancelled = asyncio.Event()
        welcome_sent = asyncio.Event()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": _hello()})

        async def receive() -> dict[str, Any]:
            try:
                return await incoming.get()
            except asyncio.CancelledError:
                receive_cancelled.set()
                raise

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            if message["type"] == "websocket.send":
                welcome_sent.set()

        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        task = asyncio.create_task(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        )
        await welcome_sent.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert receive_cancelled.is_set()
        assert outgoing[-1]["type"] == "websocket.close"
        assert outgoing[-1]["code"] == 1001
        await app.shutdown()

    asyncio.run(scenario())


def test_slow_consumer_send_is_bounded_and_cancelled() -> None:
    async def scenario() -> None:
        send_cancelled = asyncio.Event()
        incoming = iter(
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
            )
        )
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "websocket.send":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    send_cancelled.set()
                    raise
            outgoing.append(message)

        app = create_app(
            authenticator=FakeAuthenticator(),
            settings=ConnectorGatewaySettings(io_timeout_seconds=0.01),
        )
        await app.startup()
        await asyncio.wait_for(
            app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer valid-test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            ),
            timeout=0.1,
        )

        assert send_cancelled.is_set()
        assert outgoing[-1]["type"] == "websocket.close"
        await app.shutdown()

    asyncio.run(scenario())


def test_peer_disconnect_during_welcome_send_is_controlled() -> None:
    async def scenario() -> None:
        incoming = iter(
            (
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _hello()},
            )
        )
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "websocket.send":
                raise OSError("peer disconnected")
            outgoing.append(message)

        app = create_app(authenticator=FakeAuthenticator())
        await app.startup()
        await app(
            {
                "type": "websocket",
                "path": "/api/ws",
                "headers": [(b"authorization", b"Bearer valid-test-token")],
                "subprotocols": ["hermes.connector.v1"],
            },
            receive,
            send,
        )

        assert outgoing == [
            {
                "type": "websocket.accept",
                "subprotocol": "hermes.connector.v1",
            }
        ]
        await app.shutdown()

    asyncio.run(scenario())


def test_asgi_send_cancellation_still_propagates() -> None:
    async def scenario() -> None:
        messages = iter(({"type": "websocket.connect"},))

        async def receive() -> dict[str, Any]:
            return next(messages)

        accepted = False

        async def send(message: dict[str, Any]) -> None:
            nonlocal accepted
            if message["type"] == "websocket.accept":
                accepted = True
                return
            raise asyncio.CancelledError

        connection = ASGIConnectorConnection(receive, send)
        await connection.accept(timeout_seconds=0.1)

        with pytest.raises(asyncio.CancelledError):
            await connection.send_text("{}", timeout_seconds=0.1)
        assert accepted is True
        assert connection.peer_disconnected is False

    asyncio.run(scenario())


def test_asgi_send_oserror_becomes_connector_disconnect() -> None:
    async def scenario() -> None:
        messages = iter(({"type": "websocket.connect"},))

        async def receive() -> dict[str, Any]:
            return next(messages)

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "websocket.send":
                raise OSError("peer disconnected")

        connection = ASGIConnectorConnection(receive, send)
        await connection.accept(timeout_seconds=0.1)

        with pytest.raises(ConnectorDisconnected):
            await connection.send_text("{}", timeout_seconds=0.1)
        assert connection.peer_disconnected is True

    asyncio.run(scenario())


def test_asgi_receive_oserror_becomes_connector_disconnect() -> None:
    async def scenario() -> None:
        calls = 0

        async def receive() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "websocket.connect"}
            raise OSError("peer disconnected")

        async def send(_message: dict[str, Any]) -> None:
            return None

        connection = ASGIConnectorConnection(receive, send)
        await connection.accept(timeout_seconds=0.1)

        with pytest.raises(ConnectorDisconnected):
            await connection.receive_text(timeout_seconds=0.1)
        assert connection.peer_disconnected is True

    asyncio.run(scenario())


def test_asgi_receive_timeout_still_propagates() -> None:
    async def scenario() -> None:
        calls = 0

        async def receive() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "websocket.connect"}
            raise TimeoutError

        async def send(_message: dict[str, Any]) -> None:
            return None

        connection = ASGIConnectorConnection(receive, send)
        await connection.accept(timeout_seconds=0.1)

        with pytest.raises(TimeoutError):
            await connection.receive_text(timeout_seconds=0.1)
        assert connection.peer_disconnected is False

    asyncio.run(scenario())


def test_asgi_receive_cancellation_still_propagates() -> None:
    async def scenario() -> None:
        calls = 0

        async def receive() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "websocket.connect"}
            raise asyncio.CancelledError

        async def send(_message: dict[str, Any]) -> None:
            return None

        connection = ASGIConnectorConnection(receive, send)
        await connection.accept(timeout_seconds=0.1)

        with pytest.raises(asyncio.CancelledError):
            await connection.receive_text(timeout_seconds=0.1)
        assert connection.peer_disconnected is False

    asyncio.run(scenario())
