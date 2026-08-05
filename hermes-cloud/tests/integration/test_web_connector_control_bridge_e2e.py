from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

_SNAPSHOT_ROOT = os.environ.get("HERMES_INTEGRATION_SNAPSHOT_ROOT")
REPOSITORY_ROOT = (
    Path(_SNAPSHOT_ROOT)
    if _SNAPSHOT_ROOT is not None
    else Path(__file__).parents[3]
)
CONNECTOR_SOURCE = REPOSITORY_ROOT / "hermes-connector" / "src"
PLUGIN_SOURCE = REPOSITORY_ROOT / "hermes-agent-plugin" / "src"
VERIFIED_HOST_SPI_SOURCE = (
    Path(__file__).parents[1] / "fixtures/hermes_core_host_spi_v1"
)
VERIFIED_HOST_SPI_SHA256 = (
    "2b64ba5af6548823462a3fa189570389574c970c05910f28b585228494ea6619"
)
for source in (
    VERIFIED_HOST_SPI_SOURCE,
    REPOSITORY_ROOT,
    CONNECTOR_SOURCE,
    PLUGIN_SOURCE,
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.platform.macos.plugin_control_relay import (
    MacOSPluginOwnerControlChannelFactory,
)
from hermes_connector.application.owner_control_lane import (
    OwnerControlLane,
)
from tests.e2e.control_pipeline.harness import GatewayExtensionV1ControlTestHost
from tests.e2e.plugin_test_runtime import create_connector_authority_provider

from hermes_cloud.adapters.owner_control_bridge import (
    BridgeRegisteringRouteResolver,
    OwnerControlBridgeClient,
    OwnerControlBridgeServer,
)
from hermes_cloud.entrypoints.business_api import create_app
from hermes_cloud.modules.control.broker import OwnerControlBroker
from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
)
from hermes_cloud.modules.control.runtime import BrokeredControlRuntime
from hermes_cloud.modules.identity.domain import (
    Argon2PasswordHasher,
    PasswordCredential,
    RefreshSession,
    RefreshSessionUnavailable,
    WebSocketTicket,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
)
from hermes_cloud.modules.projection.domain import (
    CatalogSessionProjection,
    SessionProjection,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
DEVICE_ID = "device-1"
CLIENT_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
AGENT_ID = UUID("77777777-7777-4777-8777-777777777777")
SESSION_KEY = "session-root-1"
PROFILE = "default"
NOW = datetime.now(UTC).replace(microsecond=0)
SIGNING_KEY = b"web-control-bridge-test-signing-key-32-bytes"


class _TenantResolver:
    def tenant_for_subject(self, subject: str) -> UUID | None:
        return TENANT_ID if subject == "web@example.test" else None


class _SecretResolver:
    def resolve(self, reference: str) -> bytes:
        if reference != "test/web-control-signing":
            raise KeyError(reference)
        return SIGNING_KEY


class _IdentityRepository:
    def __init__(self) -> None:
        self.credential = PasswordCredential(
            tenant_id=TENANT_ID,
            credential_id=UUID("55555555-5555-4555-8555-555555555555"),
            user_id=USER_ID,
            subject="web@example.test",
            password_hash=Argon2PasswordHasher().hash("correct-password"),
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
        self.refresh_sessions: dict[UUID, RefreshSession] = {}
        self.tickets: dict[str, WebSocketTicket] = {}

    def credential_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject: str,
    ) -> PasswordCredential | None:
        if tenant_id == TENANT_ID and subject == self.credential.subject:
            return self.credential
        return None

    def create_refresh_session(self, value: RefreshSession) -> RefreshSession:
        self.refresh_sessions[value.refresh_session_id] = value
        return value

    def refresh_session_by_id(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
    ) -> RefreshSession | None:
        value = self.refresh_sessions.get(refresh_session_id)
        if value is None or value.tenant_id != tenant_id:
            return None
        return value

    def rotate_refresh_session(self, **_values: object) -> RefreshSession:
        raise RefreshSessionUnavailable

    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession:
        current = self.refresh_session_by_id(
            tenant_id=tenant_id,
            refresh_session_id=refresh_session_id,
        )
        if current is None or current.revoked_at is not None:
            raise RefreshSessionUnavailable
        revoked = replace(current, revoked_at=now)
        self.refresh_sessions[refresh_session_id] = revoked
        return revoked

    def issue_websocket_ticket(self, value: WebSocketTicket) -> WebSocketTicket:
        self.tickets[value.ticket_digest] = value
        return value

    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket:
        value = self.tickets.get(claim.ticket_digest)
        if (
            value is None
            or value.tenant_id != claim.tenant_id
            or value.principal_id != claim.principal_id
            or value.refresh_session_id != claim.refresh_session_id
            or value.session_id != claim.session_id
            or value.consumed_at is not None
            or value.expires_at <= now
        ):
            raise WebSocketTicketUnavailable
        consumed = replace(value, consumed_at=now)
        self.tickets[claim.ticket_digest] = consumed
        return consumed


class _ProjectionRepository:
    def __init__(self) -> None:
        self.session = SessionProjection(
            tenant_id=TENANT_ID,
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            workspace_id=UUID("66666666-6666-4666-8666-666666666666"),
            agent_id=AGENT_ID,
            profile=PROFILE,
            title="Web control bridge",
            state="active",
            revision=1,
            lineage_tip_message_id=None,
            lineage_tip_sequence=0,
            started_at=NOW,
            updated_at=NOW,
            closed_at=None,
            retention_until=NOW + timedelta(days=1),
        )

    def session_detail(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjection | None:
        if (
            tenant_id == TENANT_ID
            and user_id == USER_ID
            and session_key == SESSION_KEY
            and agent_id in {None, AGENT_ID}
            and profile in {None, PROFILE}
        ):
            return self.session
        return None


class _CatalogRepository:
    def list_agent_sessions(self, **scope: object):
        projection = self.resolve_visible_session(
            **scope,
            session_id=SESSION_ID,
        )
        return ((projection,), 1) if projection is not None else ((), 0)

    def resolve_visible_session(self, **scope: object) -> CatalogSessionProjection | None:
        if (
            scope["tenant_id"] != TENANT_ID
            or scope["user_id"] != USER_ID
            or scope["session_id"] != SESSION_ID
            or scope.get("agent_id") not in {None, AGENT_ID}
            or scope.get("profile") not in {None, PROFILE}
        ):
            return None
        return CatalogSessionProjection(
            session_id=SESSION_ID,
            agent_id=AGENT_ID,
            workspace_id=UUID("66666666-6666-4666-8666-666666666666"),
            profile=PROFILE,
            session_key=SESSION_KEY,
            runtime_generation="runtime-generation-1",
            surface="hermes-cli",
            authority_revision=1,
            available_actions=(
                "prompt.submit",
                "session.interrupt",
                "session.steer",
                "approval.respond",
                "clarify.respond",
            ),
            active=True,
        )


class _RouteResolver:
    async def resolve(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        assert context.authentication.session_key == SESSION_KEY
        assert context.authentication.profile == "default"
        assert context.authentication.client_instance_id == CLIENT_ID
        assert context.authentication.agent_id == AGENT_ID
        return ControlConnectorRoute(
            tenant_id=str(TENANT_ID),
            device_id=DEVICE_ID,
            principal_tenant_id=str(TENANT_ID),
        )


class _ConnectorLaneBridgeHandler:
    def __init__(self, lane: OwnerControlLane) -> None:
        self._lane = lane
        self._codec = ConnectorProtocolCodec()

    async def handle_bridge_request(
        self,
        *,
        peer_id: str,
        route: ControlConnectorRoute,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        del peer_id
        assert route == ControlConnectorRoute(str(TENANT_ID), DEVICE_ID)
        request = self._codec.decode_control_request_payload(payload)
        response = await self._lane.process(request)
        return json.loads(self._codec.encode_control_response(response))

    async def bridge_disconnected(self, *, peer_id: str) -> None:
        del peer_id
        await self._lane.close_all()


def _rpc(socket: object, request_id: int, method: str, params: dict[str, object]):
    socket.send_json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )
    return socket.receive_json()


def _exercise_web_control(
    application: object,
    bridge_client: OwnerControlBridgeClient,
) -> None:
    with TestClient(application, base_url="https://cloud.test") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "web@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        assert login.json() == {"ok": True}
        browser_headers = {
            "Accept": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        catalog = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions",
            params={"min_messages": 0},
            headers=browser_headers,
        )
        assert catalog.status_code == 200
        assert catalog.json()["sessions"][0]["id"] == str(SESSION_ID)
        detail = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions/{SESSION_ID}",
            headers=browser_headers,
        )
        assert detail.status_code == 200
        assert detail.json()["id"] == catalog.json()["sessions"][0]["id"]
        ticket_response = client.post(
            "/api/auth/ws-ticket",
            json={
                "connection_role": "control",
                "client_instance_id": CLIENT_ID,
                "session_id": str(SESSION_ID),
                "agent_id": str(AGENT_ID),
            },
            headers={"Origin": "https://cloud.test"},
        )
        assert ticket_response.status_code == 200
        ticket = ticket_response.json()["ticket"]
        with client.websocket_connect(
            f"/api/ws?ticket={ticket}",
            subprotocols=["hermes.tui.v1"],
        ) as socket:
            ready = socket.receive_json()["params"]["payload"]
            assert ready["control_available_methods"] == [
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
            ]
            no_lease = _rpc(
                socket,
                1,
                "session.interrupt",
                {
                    "session_id": str(SESSION_ID),
                    "client_request_id": "request-no-lease",
                    "lease_id": "not-yet-acquired",
                },
            )
            assert no_lease["error"]["code"] == 4204
            acquire = _rpc(
                socket,
                2,
                "session.control.acquire",
                {
                    "session_id": str(SESSION_ID),
                },
            )["result"]
            lease_id = acquire["lease_id"]
            assert acquire["pending_input"] is None

            wrong_lease = _rpc(
                socket,
                3,
                "session.interrupt",
                {
                    "session_id": str(SESSION_ID),
                    "client_request_id": "request-wrong-lease",
                    "lease_id": "wrong-lease",
                },
            )
            assert wrong_lease["error"]["code"] == 4206
            prompt = _rpc(
                socket,
                4,
                "prompt.submit",
                {
                    "session_id": str(SESSION_ID),
                    "lease_id": lease_id,
                    "client_request_id": "request-prompt",
                    "client_turn_id": "turn-prompt",
                    "text": "Run the focused tests",
                },
            )["result"]
            assert prompt == {
                "status": "accepted",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "server_turn_id": "server-turn-1",
            }
            assert (
                _rpc(
                    socket,
                    5,
                    "session.command.status",
                    {
                        "session_id": str(SESSION_ID),
                        "method": "prompt.submit",
                        "client_request_id": "request-prompt",
                    },
                )["result"]["status"]
                == "accepted"
            )
            for request_id, method, params in (
                (
                    6,
                    "session.steer",
                    {
                        "session_id": str(SESSION_ID),
                        "lease_id": lease_id,
                        "client_request_id": "request-shared",
                        "text": "Focus on the first failure",
                    },
                ),
                (
                    7,
                    "session.interrupt",
                    {
                        "session_id": str(SESSION_ID),
                        "lease_id": lease_id,
                        "client_request_id": "request-shared",
                    },
                ),
            ):
                assert _rpc(socket, request_id, method, params)["result"]["status"] == (
                    "accepted"
                )
            for request_id, method in (
                (20, "session.steer"),
                (21, "session.interrupt"),
            ):
                assert _rpc(
                    socket,
                    request_id,
                    "session.command.status",
                    {
                        "session_id": str(SESSION_ID),
                        "method": method,
                        "client_request_id": "request-shared",
                    },
                )["result"] == {
                    "status": "accepted",
                    "client_request_id": "request-shared",
                }

            approval = _rpc(
                socket,
                8,
                "approval.respond",
                {
                    "session_id": str(SESSION_ID),
                    "lease_id": lease_id,
                    "client_request_id": "request-approval",
                    "request_id": "pending-approval",
                    "choice": "allow_once",
                },
            )["result"]
            assert approval["kind"] == "approval"
            assert _rpc(
                socket,
                22,
                "session.command.status",
                {
                    "session_id": str(SESSION_ID),
                    "method": "approval.respond",
                    "client_request_id": "request-approval",
                },
            )["result"] == {
                "status": "accepted",
                "client_request_id": "request-approval",
            }
            status = _rpc(
                socket,
                9,
                "session.control.status",
                {"session_id": str(SESSION_ID)},
            )["result"]
            assert status["controller_kind"] == "mobile"
            assert status["pending_input"] is None
            clarify = _rpc(
                socket,
                10,
                "clarify.respond",
                {
                    "session_id": str(SESSION_ID),
                    "lease_id": lease_id,
                    "client_request_id": "request-clarify",
                    "request_id": "pending-clarify",
                    "choice_id": "choice-1",
                },
            )["result"]
            assert clarify["kind"] == "clarify"
            assert _rpc(
                socket,
                23,
                "session.command.status",
                {
                    "session_id": str(SESSION_ID),
                    "method": "clarify.respond",
                    "client_request_id": "request-clarify",
                },
            )["result"] == {
                "status": "accepted",
                "client_request_id": "request-clarify",
            }
            unknown = _rpc(
                socket,
                11,
                "session.command.status",
                {
                    "session_id": str(SESSION_ID),
                    "method": "session.interrupt",
                    "client_request_id": "request-unknown",
                },
            )
            assert unknown["error"]["code"] == 4210
            repeated_unknown = _rpc(
                socket,
                24,
                "session.command.status",
                {
                    "session_id": str(SESSION_ID),
                    "method": "session.interrupt",
                    "client_request_id": "request-unknown",
                },
            )
            assert repeated_unknown["error"]["code"] == 4210
            effect_unknown = _rpc(
                socket,
                25,
                "session.interrupt",
                {
                    "session_id": str(SESSION_ID),
                    "lease_id": lease_id,
                    "client_request_id": "request-effect-unknown",
                },
            )
            assert effect_unknown["error"]["code"] == 4307
            for request_id in (26, 27):
                unresolved = _rpc(
                    socket,
                    request_id,
                    "session.command.status",
                    {
                        "session_id": str(SESSION_ID),
                        "method": "session.interrupt",
                        "client_request_id": "request-effect-unknown",
                    },
                )
                assert unresolved["error"]["code"] == 4210
            reserved = _rpc(
                socket,
                12,
                "session.redirect",
                {
                    "session_id": str(SESSION_ID),
                    "lease_id": lease_id,
                    "client_request_id": "request-redirect",
                },
            )
            assert reserved["error"]["code"] == 4209
            renewed = _rpc(
                socket,
                13,
                "session.control.renew",
                {
                    "session_id": str(SESSION_ID),
                    "lease_id": lease_id,
                },
            )["result"]
            assert renewed["lease_id"] == lease_id
            released = _rpc(
                socket,
                14,
                "session.control.release",
                {"session_id": str(SESSION_ID), "lease_id": lease_id},
            )["result"]
            assert released["released"] is True
            released_status = _rpc(
                socket,
                15,
                "session.control.status",
                {"session_id": str(SESSION_ID)},
            )["result"]
            assert released_status["controller_kind"] == "none"
        assert client.portal is not None
        client.portal.call(bridge_client.close)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS UDS backend")
async def test_cookie_to_cloud_bridge_real_connector_lane_owner_actions() -> None:
    host_spi = VERIFIED_HOST_SPI_SOURCE / "hermes_cli/extension_host_v1.py"
    assert host_spi.is_file()
    assert hashlib.sha256(host_spi.read_bytes()).hexdigest() == VERIFIED_HOST_SPI_SHA256
    temporary = tempfile.TemporaryDirectory(prefix="hc-web-", dir="/tmp")
    runtime_directory = Path(temporary.name).resolve(strict=True)
    runtime_directory.chmod(0o700)
    socket_path = runtime_directory / "owner-control.sock"
    control_socket_root = Path("/tmp").resolve(strict=True) / (
        f"hc-plugin-control-{os.getpid()}-{runtime_directory.name[-8:]}"
    )
    plugin_paths = MacOSLocalGatewayPaths(
        local_gateway_registry_directory=runtime_directory / "local-registry",
        local_gateway_socket_directory=control_socket_root / "local",
        control_registry_directory=runtime_directory / "control-registry",
        control_socket_directory=control_socket_root / "control",
        observer_registry_directory=runtime_directory / "observer-registry",
        observer_socket_directory=control_socket_root / "observer",
    )
    identity = _IdentityRepository()
    projection = _ProjectionRepository()
    plugin_host = GatewayExtensionV1ControlTestHost(
        plugin_paths,
        profile="default",
        runtime_generation="runtime-generation-1",
        effect_unknown_on_call={"session.interrupt": 2},
    )
    plugin_factory = MacOSPluginOwnerControlChannelFactory(
        registry_directory=plugin_paths.control_registry_directory,
        socket_directory=plugin_paths.control_socket_directory,
        profile="default",
        provider="hermes-cloud",
        authority=create_connector_authority_provider(plugin_host.authority),
    )
    lane = OwnerControlLane(factory=plugin_factory, utc_now=lambda: NOW)
    bridge_server = OwnerControlBridgeServer(
        socket_path=socket_path,
        handler=_ConnectorLaneBridgeHandler(lane),
    )
    bridge_client = OwnerControlBridgeClient(socket_path=socket_path)
    broker = OwnerControlBroker()
    runtime = BrokeredControlRuntime(
        broker=broker,
        route_resolver=BridgeRegisteringRouteResolver(
            delegate=_RouteResolver(),
            broker=broker,
            client=bridge_client,
            broker_connection_id="77777777-7777-4777-8777-777777777777",
        ),
        now=lambda: NOW,
    )
    application = create_app(
        identity_repository=identity,
        projection_repository=projection,
        session_catalog_repository=_CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "test/web-control-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
        control_runtime=runtime,
    )

    plugin_host.start()
    await bridge_server.start()
    try:
        await asyncio.to_thread(
            _exercise_web_control,
            application,
            bridge_client,
        )
    finally:
        await bridge_server.stop()
        await lane.close_all()
        plugin_host.close()
        temporary.cleanup()

    assert plugin_host.owner_calls == [
        "prompt.submit",
        "session.steer",
        "session.interrupt",
        "approval.respond",
        "clarify.respond",
        "session.interrupt",
    ]
    assert all(
        ticket.ticket_digest == digest for digest, ticket in identity.tickets.items()
    )
