"""Cloud Connector Envelope v1 session coordinator, including Observer v1/v2."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hermes_cloud.application.capabilities import CAPABILITY_CATALOG
from hermes_cloud.contracts.observer_v2 import require_payload
from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import (
    ConnectorAuthenticationExpired,
    ConnectorAuthorizationRevoked,
    ConnectorAuthorizationSuspended,
    ConnectorAuthorizationUnavailable,
    ConnectorIdentity,
    ConnectorIdentityMismatch,
    ConnectorObserverReceiptDelivery,
    ConnectorObserverRejected,
    ConnectorResumeResolution,
    ConnectorSessionCatalogReceiptDelivery,
    ConnectorUnsupportedData,
    ConnectorUnsupportedMessage,
)
from hermes_cloud.domain.contract_errors import CoreContractError
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.ports.connector_gateway import (
    ConnectorAuthenticator,
    ConnectorCommandRouter,
    ConnectorConnection,
    ConnectorObserverIngress,
    ConnectorObserverReceiptRouter,
    ConnectorObserverSubscriptionRouter,
    ConnectorOwnerControlRouter,
    ConnectorProtocolCodec,
    ConnectorResumeResolver,
    ConnectorSessionCatalogIngress,
    ConnectorTransportCursorAuthority,
)

_LOGGER = logging.getLogger(__name__)


class ConnectorSessionError(RuntimeError):
    """A Connector session failed without exposing untrusted frame content."""


class ConnectorCapabilityUnavailable(ConnectorSessionError):
    """A required Connector capability is unavailable."""


class ConnectorHeartbeatExpired(ConnectorSessionError):
    """The Connector failed to send a heartbeat before the deadline."""


@dataclass(slots=True)
class _SessionCursors:
    next_connector_sequence: int
    next_cloud_sequence: int


@dataclass(frozen=True, slots=True)
class ConnectorGatewaySettings:
    available_capabilities: tuple[str, ...] = (
        "session.catalog.v1",
        "session.observe",
        "session.observe.output-parity.v1",
    )
    server_generation: str = "hermes-cloud-foundation"
    negotiation_timeout_seconds: float = 10.0
    io_timeout_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 45.0
    heartbeat_interval_ms: int = 20_000
    max_in_flight: int = 64
    resume_timeout_seconds: float = 3.0
    router_timeout_seconds: float = 3.0
    transport_ownership_lease_seconds: float = 90.0

    def __post_init__(self) -> None:
        capabilities = self.available_capabilities
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("available capabilities must be unique")
        if not set(capabilities) <= set(CAPABILITY_CATALOG):
            raise ValueError("available capabilities are outside the catalog")
        if not self.server_generation:
            raise ValueError("server generation is required")
        if (
            not math.isfinite(self.negotiation_timeout_seconds)
            or self.negotiation_timeout_seconds <= 0
        ):
            raise ValueError("negotiation timeout must be finite and positive")
        if not math.isfinite(self.io_timeout_seconds) or self.io_timeout_seconds <= 0:
            raise ValueError("I/O timeout must be finite and positive")
        if (
            not math.isfinite(self.heartbeat_timeout_seconds)
            or self.heartbeat_timeout_seconds <= 0
        ):
            raise ValueError("heartbeat timeout must be finite and positive")
        if type(self.heartbeat_interval_ms) is not int:
            raise ValueError("heartbeat interval must be an integer")
        if not 5_000 <= self.heartbeat_interval_ms <= 120_000:
            raise ValueError("heartbeat interval is outside contract bounds")
        if type(self.max_in_flight) is not int:
            raise ValueError("max in flight must be an integer")
        if not 1 <= self.max_in_flight <= 256:
            raise ValueError("max in flight is outside contract bounds")
        if (
            not math.isfinite(self.resume_timeout_seconds)
            or self.resume_timeout_seconds <= 0
        ):
            raise ValueError("resume timeout must be finite and positive")
        if (
            not math.isfinite(self.router_timeout_seconds)
            or self.router_timeout_seconds <= 0
        ):
            raise ValueError("router timeout must be finite and positive")
        if (
            not math.isfinite(self.transport_ownership_lease_seconds)
            or self.transport_ownership_lease_seconds <= 0
        ):
            raise ValueError("ownership lease must be finite and positive")
        required_ownership_window = max(
            self.negotiation_timeout_seconds
            + self.io_timeout_seconds
            + (2 * self.router_timeout_seconds),
            self.heartbeat_timeout_seconds
            + (self.heartbeat_interval_ms / 1000)
            + self.io_timeout_seconds,
        )
        if self.transport_ownership_lease_seconds <= required_ownership_window:
            raise ValueError(
                "ownership lease must exceed negotiation and heartbeat windows"
            )


class ConnectorGatewayService:
    """Authenticate and coordinate one bounded Connector WebSocket session."""

    def __init__(
        self,
        *,
        authenticator: ConnectorAuthenticator,
        codec: ConnectorProtocolCodec,
        settings: ConnectorGatewaySettings,
        resume_resolver: ConnectorResumeResolver | None = None,
        transport_cursor_authority: ConnectorTransportCursorAuthority | None = None,
        command_router: ConnectorCommandRouter | None = None,
        owner_control_router: ConnectorOwnerControlRouter | None = None,
        observer_ingress: ConnectorObserverIngress | None = None,
        session_catalog_ingress: ConnectorSessionCatalogIngress | None = None,
        observer_receipt_router: ConnectorObserverReceiptRouter | None = None,
        observer_subscription_router: ConnectorObserverSubscriptionRouter | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._authenticator = authenticator
        self._codec = codec
        self._settings = settings
        self._transport_cursor_authority = transport_cursor_authority
        self._resume_resolver = resume_resolver or transport_cursor_authority
        self._command_router = command_router
        self._owner_control_router = owner_control_router
        self._observer_ingress = observer_ingress
        if session_catalog_ingress is not None and any(
            not callable(getattr(session_catalog_ingress, method, None))
            for method in (
                "accept_snapshot_page_and_advance",
                "accept_event_and_advance",
                "next_pending_receipt",
                "reserve_pending_receipt_and_advance",
                "mark_receipt_sent",
                "confirm_receipts_through_cursor",
            )
        ):
            raise TypeError(
                "session catalog ingress requires atomic transport cursor methods"
            )
        self._session_catalog_ingress = session_catalog_ingress
        self._observer_receipt_router = observer_receipt_router
        self._observer_subscription_router = observer_subscription_router
        self._utc_now = utc_now
        self._uuid_factory = uuid_factory
        self._sleep = sleep
        self._transport_cleanup_failure_count = 0
        self._transport_cleanup_reconcile_required_count = 0

    @property
    def transport_cleanup_failure_count(self) -> int:
        return self._transport_cleanup_failure_count

    @property
    def transport_cleanup_reconcile_required_count(self) -> int:
        return self._transport_cleanup_reconcile_required_count

    async def handle(
        self,
        bearer_token: str | None,
        connection: ConnectorConnection,
    ) -> None:
        if bearer_token is None:
            await self._close(connection, 1008, "authentication_required")
            return
        connected_binding: (
            tuple[
                ConnectorIdentity,
                str,
                str,
            ]
            | None
        ) = None
        command_router_registered = False
        owner_control_router_registered = False
        observer_subscription_router_registered = False
        transport_authority_registered = False
        transport_authority_prepared = False
        welcome_sent = False
        try:
            async with asyncio.timeout(self._settings.negotiation_timeout_seconds):
                authenticated = await self._authenticator.authenticate(bearer_token)
            identity = self._identity(authenticated)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fail-closed authentication boundary
            await self._close(connection, 1008, "authentication_failed")
            return

        try:
            await connection.accept(
                timeout_seconds=self._settings.negotiation_timeout_seconds
            )
            raw_hello = await connection.receive_text(
                timeout_seconds=self._settings.negotiation_timeout_seconds
            )
            envelope = self._codec.decode_connector_frame(raw_hello)
            if envelope.message_type != "connector.hello":
                raise ConnectorSessionError("first connector frame must be hello")
            self._bind_identity(envelope, identity)
            hello = self._codec.decode_hello(envelope.payload)
            if envelope.sequence != hello.resume.next_outbound_sequence:
                raise ConnectorSessionError(
                    "hello sequence does not match its resume cursor"
                )
            accepted, unavailable = self._negotiate(
                hello.required_capabilities,
                hello.optional_capabilities,
                identity=identity,
            )
            resolution = await self._resolve_resume(
                identity,
                hello.resume,
                connector_instance_id=hello.connector_instance_id,
                runtime_generation=hello.runtime_generation,
            )
            connection_id = str(self._uuid_factory())
            welcome_sequence = hello.resume.next_inbound_sequence
            active_next_connector_sequence = resolution.next_connector_sequence
            active_next_cloud_sequence = resolution.next_cloud_sequence
            if resolution.handshake_disposition == "advance":
                active_next_connector_sequence += 1
                active_next_cloud_sequence += 1
            welcome = CloudEnvelope(
                contract_version=1,
                message_id=str(self._uuid_factory()),
                message_type="connector.welcome",
                tenant_id=identity.tenant_id,
                device_id=identity.device_id,
                sequence=welcome_sequence,
                sent_at=self._timestamp(),
                payload={
                    "connection_id": connection_id,
                    "server_generation": self._settings.server_generation,
                    "server_time": self._timestamp(),
                    "accepted_capabilities": list(accepted),
                    "unavailable_optional_capabilities": list(unavailable),
                    "resume_decision": resolution.decision,
                    "next_connector_sequence": active_next_connector_sequence,
                    "next_cloud_sequence": active_next_cloud_sequence,
                    "heartbeat_interval_ms": (self._settings.heartbeat_interval_ms),
                    "max_in_flight": self._settings.max_in_flight,
                },
            )
            if self._transport_cursor_authority is not None:
                try:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await self._transport_cursor_authority.prepare_session(
                            identity=identity,
                            connection_id=connection_id,
                            connector_instance_id=hello.connector_instance_id,
                            runtime_generation=hello.runtime_generation,
                            resume_decision=resolution.decision,
                            handshake_disposition=(resolution.handshake_disposition),
                            previous_connection_id=(
                                hello.resume.previous_connection_id
                                if resolution.decision == "resumed"
                                else None
                            ),
                            expected_next_connector_sequence=(
                                resolution.next_connector_sequence
                            ),
                            expected_next_cloud_sequence=(
                                resolution.next_cloud_sequence
                            ),
                            next_connector_sequence=(active_next_connector_sequence),
                            next_cloud_sequence=active_next_cloud_sequence,
                        )
                except RuntimeError:
                    raise ConnectorSessionError(
                        "Connector transport ownership changed"
                    ) from None
                transport_authority_prepared = True
            await connection.send_text(
                self._codec.encode_connector_frame(welcome),
                timeout_seconds=self._settings.io_timeout_seconds,
            )
            welcome_sent = True
            if self._transport_cursor_authority is not None:
                try:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await self._transport_cursor_authority.confirm_session(
                            identity=identity,
                            connection_id=connection_id,
                            connector_instance_id=hello.connector_instance_id,
                            runtime_generation=hello.runtime_generation,
                        )
                except RuntimeError:
                    raise ConnectorSessionError(
                        "Connector transport ownership changed"
                    ) from None
                connected_binding = (
                    identity,
                    connection_id,
                    hello.connector_instance_id,
                )
                transport_authority_registered = True
            command_router = (
                self._command_router if "session.control" in accepted else None
            )
            owner_control_router = (
                self._owner_control_router if "session.control" in accepted else None
            )
            if command_router is not None:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await command_router.connector_connected(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=hello.connector_instance_id,
                        runtime_generation=hello.runtime_generation,
                    )
                connected_binding = (
                    identity,
                    connection_id,
                    hello.connector_instance_id,
                )
                command_router_registered = True
            if owner_control_router is not None:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await owner_control_router.connector_connected(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=hello.connector_instance_id,
                        runtime_generation=hello.runtime_generation,
                    )
                connected_binding = (
                    identity,
                    connection_id,
                    hello.connector_instance_id,
                )
                owner_control_router_registered = True
            observer_subscription_router = (
                self._observer_subscription_router
                if "session.observe" in accepted
                else None
            )
            if observer_subscription_router is not None:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await observer_subscription_router.connector_connected(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=hello.connector_instance_id,
                        runtime_generation=hello.runtime_generation,
                    )
                connected_binding = (
                    identity,
                    connection_id,
                    hello.connector_instance_id,
                )
                observer_subscription_router_registered = True
            await self._run_active_session(
                connection,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=hello.connector_instance_id,
                runtime_generation=hello.runtime_generation,
                observer_v2_active=("session.observe.output-parity.v1" in accepted),
                command_router=command_router,
                owner_control_router=owner_control_router,
                observer_subscription_router=observer_subscription_router,
                observer_ingress=(
                    self._observer_ingress if "session.observe" in accepted else None
                ),
                session_catalog_ingress=(
                    self._session_catalog_ingress
                    if "session.catalog.v1" in accepted
                    else None
                ),
                observer_receipt_router=(
                    self._observer_receipt_router
                    if "session.observe" in accepted
                    else None
                ),
                session_state=(
                    "reconciling"
                    if resolution.decision == "reset_required"
                    else "active"
                ),
                cursors=_SessionCursors(
                    next_connector_sequence=active_next_connector_sequence,
                    next_cloud_sequence=active_next_cloud_sequence,
                ),
            )
        except asyncio.CancelledError:
            await self._close(connection, 1001, "server_shutdown")
            raise
        except ConnectionError:
            return
        except ConnectorIdentityMismatch:
            await self._close(connection, 1008, "identity_mismatch")
        except ConnectorCapabilityUnavailable:
            await self._close(connection, 1008, "capability_unavailable")
        except ConnectorHeartbeatExpired:
            await self._close(connection, 1001, "heartbeat_timeout")
        except ConnectorAuthorizationRevoked:
            await self._close(connection, 1008, "device_authorization_revoked")
        except ConnectorAuthorizationSuspended:
            await self._close(connection, 1008, "device_authorization_suspended")
        except ConnectorAuthorizationUnavailable:
            await self._close(connection, 1011, "authorization_recheck_unavailable")
        except ConnectorAuthenticationExpired:
            await self._close(connection, 1008, "authentication_expired")
        except (ConnectorUnsupportedData, ConnectorUnsupportedMessage):
            await self._close(connection, 1003, "unsupported_data")
        except CoreContractError as error:
            if error.category == "frame_too_large":
                await self._close(connection, 1009, "frame_too_large")
            else:
                await self._close(connection, 1002, "protocol_violation")
        except (ConnectorSessionError, ValueError):
            await self._close(connection, 1002, "protocol_violation")
        except TimeoutError:
            await self._close(connection, 1008, "deadline_exceeded")
        finally:
            if (
                connected_binding is None
                and transport_authority_prepared
                and not welcome_sent
                and self._transport_cursor_authority is not None
            ):
                await self._abort_transport_authority(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=hello.connector_instance_id,
                )
            if (
                connected_binding is not None
                and transport_authority_registered
                and self._transport_cursor_authority is not None
            ):
                bound_identity, bound_connection_id, bound_instance_id = (
                    connected_binding
                )
                await self._disconnect_transport_authority(
                    identity=bound_identity,
                    connection_id=bound_connection_id,
                    connector_instance_id=bound_instance_id,
                )
            if (
                connected_binding is not None
                and command_router_registered
                and self._command_router is not None
            ):
                bound_identity, bound_connection_id, bound_instance_id = (
                    connected_binding
                )
                try:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await self._command_router.connector_disconnected(
                            identity=bound_identity,
                            connection_id=bound_connection_id,
                            connector_instance_id=bound_instance_id,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001,S110 - cleanup is best effort
                    pass
            if (
                connected_binding is not None
                and owner_control_router_registered
                and self._owner_control_router is not None
            ):
                bound_identity, bound_connection_id, bound_instance_id = (
                    connected_binding
                )
                try:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await self._owner_control_router.connector_disconnected(
                            identity=bound_identity,
                            connection_id=bound_connection_id,
                            connector_instance_id=bound_instance_id,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001,S110 - cleanup is best effort
                    pass
            if (
                connected_binding is not None
                and observer_subscription_router_registered
                and self._observer_subscription_router is not None
            ):
                bound_identity, bound_connection_id, bound_instance_id = (
                    connected_binding
                )
                try:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await self._observer_subscription_router.connector_disconnected(
                            identity=bound_identity,
                            connection_id=bound_connection_id,
                            connector_instance_id=bound_instance_id,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001,S110 - cleanup is best effort
                    pass
            if not connection.peer_disconnected:
                await self._close(connection, 1000, "")

    async def _abort_transport_authority(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        authority = self._transport_cursor_authority
        if authority is None:
            return
        try:
            async with asyncio.timeout(self._settings.router_timeout_seconds):
                await authority.abort_session(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - cleanup is observable
            self._transport_cleanup_failure_count += 1
            self._transport_cleanup_reconcile_required_count += 1
            _LOGGER.warning(
                "Connector transport handshake cleanup failed",
                extra={
                    "event": "connector_transport_handshake_abort_failed",
                    "error_type": type(error).__name__,
                },
            )

    async def _disconnect_transport_authority(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
    ) -> None:
        authority = self._transport_cursor_authority
        if authority is None:
            return
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await authority.disconnect_session(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - cleanup is observable
                terminal = attempt == 2
                self._transport_cleanup_failure_count += 1
                if terminal:
                    self._transport_cleanup_reconcile_required_count += 1
                _LOGGER.warning(
                    "Connector transport disconnect cleanup failed",
                    extra={
                        "event": "connector_transport_disconnect_failed",
                        "attempt": attempt,
                        "terminal": terminal,
                        "error_type": type(error).__name__,
                    },
                )

    async def _run_active_session(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        observer_v2_active: bool,
        command_router: ConnectorCommandRouter | None,
        owner_control_router: ConnectorOwnerControlRouter | None,
        observer_subscription_router: ConnectorObserverSubscriptionRouter | None,
        observer_ingress: ConnectorObserverIngress | None,
        session_catalog_ingress: ConnectorSessionCatalogIngress | None,
        observer_receipt_router: ConnectorObserverReceiptRouter | None,
        session_state: str,
        cursors: _SessionCursors,
    ) -> None:
        cursor_lock = asyncio.Lock()
        receiver = asyncio.create_task(
            self._receive_until_disconnect(
                connection,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                observer_v2_active=observer_v2_active,
                command_router=command_router,
                owner_control_router=owner_control_router,
                observer_subscription_router=observer_subscription_router,
                observer_ingress=observer_ingress,
                session_catalog_ingress=session_catalog_ingress,
                observer_receipt_router=observer_receipt_router,
                cursors=cursors,
                cursor_lock=cursor_lock,
            )
        )
        heartbeat = asyncio.create_task(
            self._send_heartbeats(
                connection,
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                session_state=session_state,
                cursors=cursors,
                cursor_lock=cursor_lock,
            )
        )
        tasks = {receiver, heartbeat}
        if session_catalog_ingress is not None:
            tasks.add(
                asyncio.create_task(
                    self._send_pending_catalog_receipts(
                        connection,
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        ingress=session_catalog_ingress,
                        cursors=cursors,
                        cursor_lock=cursor_lock,
                    )
                )
            )
        if command_router is not None:
            tasks.add(
                asyncio.create_task(
                    self._send_commands(
                        connection,
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        command_router=command_router,
                        cursors=cursors,
                        cursor_lock=cursor_lock,
                    )
                )
            )
        if owner_control_router is not None:
            tasks.add(
                asyncio.create_task(
                    self._send_owner_control(
                        connection,
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        owner_control_router=owner_control_router,
                        cursors=cursors,
                        cursor_lock=cursor_lock,
                    )
                )
            )
        if observer_subscription_router is not None:
            tasks.add(
                asyncio.create_task(
                    self._send_observer_subscriptions(
                        connection,
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        observer_v2_active=observer_v2_active,
                        router=observer_subscription_router,
                        cursors=cursors,
                        cursor_lock=cursor_lock,
                    )
                )
            )
        if observer_receipt_router is not None:
            tasks.add(
                asyncio.create_task(
                    self._send_pending_observer_receipts(
                        connection,
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        observer_v2_active=observer_v2_active,
                        router=observer_receipt_router,
                        cursors=cursors,
                        cursor_lock=cursor_lock,
                    )
                )
            )
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive_until_disconnect(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        observer_v2_active: bool,
        command_router: ConnectorCommandRouter | None,
        owner_control_router: ConnectorOwnerControlRouter | None,
        observer_subscription_router: ConnectorObserverSubscriptionRouter | None,
        observer_ingress: ConnectorObserverIngress | None,
        session_catalog_ingress: ConnectorSessionCatalogIngress | None,
        observer_receipt_router: ConnectorObserverReceiptRouter | None,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        while True:
            try:
                raw = await connection.receive_text(
                    timeout_seconds=self._settings.heartbeat_timeout_seconds
                )
            except TimeoutError:
                raise ConnectorHeartbeatExpired() from None
            envelope = self._codec.decode_connector_frame(raw)
            self._bind_identity(envelope, identity)
            if (
                session_catalog_ingress is not None
                and envelope.message_type
                in {"session.catalog.snapshot.page", "session.catalog.event"}
            ):
                async with cursor_lock:
                    if envelope.sequence != cursors.next_connector_sequence:
                        raise ConnectorSessionError(
                            "connector catalog cursor is invalid"
                        )
                    async with asyncio.timeout(
                        self._settings.router_timeout_seconds
                    ):
                        if envelope.message_type == "session.catalog.snapshot.page":
                            catalog_payload = (
                                self._codec.decode_session_catalog_snapshot_page(
                                    envelope.payload
                                )
                            )
                            receipt = await session_catalog_ingress.accept_snapshot_page_and_advance(
                                identity=identity,
                                connection_id=connection_id,
                                connector_instance_id=connector_instance_id,
                                runtime_generation=runtime_generation,
                                envelope=envelope,
                                payload=catalog_payload,
                                expected_next_connector_sequence=(
                                    cursors.next_connector_sequence
                                ),
                                expected_next_cloud_sequence=(
                                    cursors.next_cloud_sequence
                                ),
                            )
                        else:
                            catalog_payload = (
                                self._codec.decode_session_catalog_event(
                                    envelope.payload
                                )
                            )
                            receipt = await session_catalog_ingress.accept_event_and_advance(
                                identity=identity,
                                connection_id=connection_id,
                                connector_instance_id=connector_instance_id,
                                runtime_generation=runtime_generation,
                                envelope=envelope,
                                payload=catalog_payload,
                                expected_next_connector_sequence=(
                                    cursors.next_connector_sequence
                                ),
                                expected_next_cloud_sequence=(
                                    cursors.next_cloud_sequence
                                ),
                            )
                    next_cloud_sequence = cursors.next_cloud_sequence
                    if receipt is not None:
                        await self._send_catalog_receipt(
                            connection,
                            identity=identity,
                            delivery=receipt,
                        )
                        await session_catalog_ingress.mark_receipt_sent(
                            identity=identity,
                            connection_id=connection_id,
                            connector_instance_id=connector_instance_id,
                            runtime_generation=runtime_generation,
                            catalog_message_id=receipt.catalog_message_id,
                            message_id=receipt.message_id,
                            receipt_sequence=receipt.sequence,
                        )
                        next_cloud_sequence += 1
                    cursors.next_connector_sequence += 1
                    cursors.next_cloud_sequence = next_cloud_sequence
                continue
            observer_message_types = (
                {"session.snapshot.v2", "session.event.v2"}
                if observer_v2_active
                else {"session.snapshot", "session.event"}
            )
            all_observer_message_types = {
                "session.snapshot",
                "session.event",
                "session.snapshot.v2",
                "session.event.v2",
            }
            if (
                observer_ingress is not None
                and envelope.message_type in all_observer_message_types
            ):
                if envelope.message_type not in observer_message_types:
                    raise ConnectorSessionError(
                        "connector observer contract changed midstream"
                    )
                async with cursor_lock:
                    if envelope.sequence != cursors.next_connector_sequence:
                        raise ConnectorSessionError(
                            "connector observer cursor is invalid"
                        )
                observer_payload = None
                try:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        if envelope.message_type in {
                            "session.snapshot",
                            "session.snapshot.v2",
                        }:
                            snapshot = self._codec.decode_session_snapshot(
                                envelope.payload
                            )
                            observer_payload = snapshot
                            expected_contract = (
                                2
                                if envelope.message_type == "session.snapshot.v2"
                                else 1
                            )
                            if snapshot.observer_contract != expected_contract:
                                raise ConnectorSessionError(
                                    "observer snapshot contract does not match message type"
                                )
                            if snapshot.runtime_generation != runtime_generation:
                                raise ConnectorSessionError(
                                    "observer snapshot generation does not match"
                                )
                            await observer_ingress.accept_snapshot(
                                identity=identity,
                                connection_id=connection_id,
                                connector_instance_id=connector_instance_id,
                                runtime_generation=runtime_generation,
                                envelope=envelope,
                                payload=snapshot,
                            )
                        else:
                            event = self._codec.decode_session_event(envelope.payload)
                            observer_payload = event
                            expected_contract = (
                                2 if envelope.message_type == "session.event.v2" else 1
                            )
                            if event.observer_contract != expected_contract:
                                raise ConnectorSessionError(
                                    "observer event contract does not match message type"
                                )
                            if event.runtime_generation != runtime_generation:
                                raise ConnectorSessionError(
                                    "observer event generation does not match"
                                )
                            await observer_ingress.accept_event(
                                identity=identity,
                                connection_id=connection_id,
                                connector_instance_id=connector_instance_id,
                                runtime_generation=runtime_generation,
                                envelope=envelope,
                                payload=event,
                            )
                except ConnectorObserverRejected as rejected:
                    if observer_payload is None:
                        raise ConnectorSessionError(
                            "observer rejection has no decoded binding"
                        ) from rejected
                    await self._send_observer_receipt(
                        connection,
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        envelope=envelope,
                        payload=observer_payload,
                        receipt_type="stream.nack",
                        observer_v2_active=observer_v2_active,
                        rejection=rejected,
                        cursors=cursors,
                        cursor_lock=cursor_lock,
                        receipt_router=observer_receipt_router,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except (ConnectorSessionError, CoreContractError):
                    raise
                except Exception as error:
                    raise ConnectorSessionError(
                        "observer ingress commit failed"
                    ) from error
                if observer_payload is None:
                    raise ConnectorSessionError(
                        "observer commit has no decoded binding"
                    )
                await self._send_observer_receipt(
                    connection,
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    envelope=envelope,
                    payload=observer_payload,
                    receipt_type="stream.ack",
                    observer_v2_active=observer_v2_active,
                    rejection=None,
                    cursors=cursors,
                    cursor_lock=cursor_lock,
                    receipt_router=observer_receipt_router,
                )
                continue
            if (
                owner_control_router is not None
                and envelope.message_type == "control.response"
            ):
                request_id = envelope.payload.get("request_id")
                if (
                    not isinstance(request_id, str)
                    or envelope.idempotency_key != request_id
                ):
                    raise ConnectorSessionError(
                        "connector control response correlation is invalid"
                    )
                async with cursor_lock:
                    if envelope.sequence != cursors.next_connector_sequence:
                        raise ConnectorSessionError(
                            "connector control response cursor is invalid"
                        )
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    accepted = await owner_control_router.accept_control_response(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        payload=envelope.payload,
                    )
                if not accepted:
                    raise ConnectorSessionError(
                        "connector control response is unmatched"
                    )
                async with cursor_lock:
                    if envelope.sequence != cursors.next_connector_sequence:
                        raise ConnectorSessionError(
                            "connector control response cursor changed"
                        )
                    await self._commit_cursor_advance(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        cursors=cursors,
                        next_connector_sequence=cursors.next_connector_sequence + 1,
                        next_cloud_sequence=cursors.next_cloud_sequence,
                    )
                continue
            if command_router is not None and envelope.message_type in {
                "command.receipt",
                "command.result",
            }:
                async with cursor_lock:
                    if envelope.sequence != cursors.next_connector_sequence:
                        raise ConnectorSessionError(
                            "connector command response cursor is invalid"
                        )
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await command_router.accept_connector_response(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        envelope=envelope,
                    )
                async with cursor_lock:
                    if envelope.sequence != cursors.next_connector_sequence:
                        raise ConnectorSessionError(
                            "connector command response cursor changed"
                        )
                    await self._commit_cursor_advance(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        cursors=cursors,
                        next_connector_sequence=cursors.next_connector_sequence + 1,
                        next_cloud_sequence=cursors.next_cloud_sequence,
                    )
                continue
            if envelope.message_type != "connector.heartbeat":
                raise ConnectorUnsupportedMessage(
                    "connector sent an unsupported session message"
                )
            heartbeat = self._codec.decode_heartbeat(envelope.payload)
            async with cursor_lock:
                if (
                    envelope.sequence != cursors.next_connector_sequence
                    or heartbeat.connection_id != connection_id
                    or heartbeat.sender_role != "connector"
                    or heartbeat.next_outbound_sequence
                    != cursors.next_connector_sequence
                    or heartbeat.next_inbound_sequence != cursors.next_cloud_sequence
                ):
                    raise ConnectorSessionError("connector heartbeat cursor is invalid")
            if command_router is not None:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await command_router.connector_heartbeat(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        next_connector_sequence=(heartbeat.next_outbound_sequence),
                        next_cloud_sequence=heartbeat.next_inbound_sequence,
                    )
            if observer_subscription_router is not None:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await observer_subscription_router.connector_heartbeat(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        next_connector_sequence=(heartbeat.next_outbound_sequence),
                        next_cloud_sequence=heartbeat.next_inbound_sequence,
                    )
            async with cursor_lock:
                if envelope.sequence != cursors.next_connector_sequence:
                    raise ConnectorSessionError("connector heartbeat cursor changed")
                if observer_receipt_router is not None:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await observer_receipt_router.confirm_through_cursor(
                            identity=identity,
                            connection_id=connection_id,
                            durable_next_inbound_sequence=(
                                heartbeat.next_inbound_sequence
                            ),
                        )
                if session_catalog_ingress is not None:
                    async with asyncio.timeout(self._settings.router_timeout_seconds):
                        await session_catalog_ingress.confirm_receipts_through_cursor(
                            identity=identity,
                            connection_id=connection_id,
                            durable_next_inbound_sequence=(
                                heartbeat.next_inbound_sequence
                            ),
                        )
                await self._commit_cursor_advance(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    cursors=cursors,
                    next_connector_sequence=cursors.next_connector_sequence + 1,
                    next_cloud_sequence=cursors.next_cloud_sequence,
                )

    async def _send_observer_receipt(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        envelope: CloudEnvelope,
        payload: object,
        receipt_type: str,
        observer_v2_active: bool,
        rejection: ConnectorObserverRejected | None,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
        receipt_router: ConnectorObserverReceiptRouter | None,
    ) -> None:
        receipt_payload: dict[str, object] = {
            "observer_message_id": envelope.message_id,
            "payload_digest": canonical_payload_digest(envelope.payload),
            "connector_sequence": envelope.sequence,
            "observer_message_type": envelope.message_type,
            "profile": payload.profile,
            "session_key": payload.session_key,
            "runtime_generation": payload.runtime_generation,
            "runtime_session_id": payload.runtime_session_id,
            "event_sequence": payload.event_sequence,
        }
        if observer_v2_active:
            receipt_payload["observer_contract"] = 2
        timestamp = self._timestamp()
        if rejection is None:
            receipt_payload["committed_at"] = timestamp
        else:
            receipt_payload.update(
                {
                    "reason": rejection.reason,
                    "expected_event_sequence": (rejection.expected_event_sequence),
                    "recovery": rejection.recovery,
                    "rejected_at": timestamp,
                }
            )
        outbound_receipt_type = (
            f"{receipt_type}.v2" if observer_v2_active else receipt_type
        )
        if observer_v2_active:
            require_payload(outbound_receipt_type.replace(".", "-"), receipt_payload)
        async with cursor_lock:
            if envelope.sequence != cursors.next_connector_sequence:
                raise ConnectorSessionError("connector observer cursor changed")
            sequence = cursors.next_cloud_sequence
            if receipt_router is None:
                receipt = CloudEnvelope(
                    contract_version=1,
                    message_id=str(self._uuid_factory()),
                    message_type=outbound_receipt_type,
                    tenant_id=identity.tenant_id,
                    device_id=identity.device_id,
                    sequence=sequence,
                    sent_at=timestamp,
                    idempotency_key=envelope.message_id,
                    payload=receipt_payload,
                )
            else:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    delivery = await receipt_router.stage_and_reserve(
                        identity=identity,
                        connection_id=connection_id,
                        observer_message_id=envelope.message_id,
                        receipt_type=receipt_type,
                        payload=receipt_payload,
                        sequence=sequence,
                    )
                receipt = self._observer_receipt_envelope(
                    identity,
                    delivery,
                    observer_v2_active=observer_v2_active,
                )
            await connection.send_text(
                self._codec.encode_connector_frame(receipt),
                timeout_seconds=self._settings.io_timeout_seconds,
            )
            if receipt_router is not None:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await receipt_router.mark_sent(
                        identity=identity,
                        connection_id=connection_id,
                        observer_message_id=envelope.message_id,
                        message_id=receipt.message_id,
                        sequence=sequence,
                    )
            await self._commit_cursor_advance(
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
                cursors=cursors,
                next_connector_sequence=cursors.next_connector_sequence + 1,
                next_cloud_sequence=cursors.next_cloud_sequence + 1,
            )

    async def _send_catalog_receipt(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        delivery: ConnectorSessionCatalogReceiptDelivery,
    ) -> None:
        receipt = CloudEnvelope(
            contract_version=1,
            message_id=delivery.message_id,
            message_type=delivery.message_type,
            tenant_id=identity.tenant_id,
            device_id=identity.device_id,
            sequence=delivery.sequence,
            sent_at=delivery.sent_at,
            idempotency_key=delivery.catalog_message_id,
            payload=dict(delivery.payload),
        )
        await connection.send_text(
            self._codec.encode_connector_frame(receipt),
            timeout_seconds=self._settings.io_timeout_seconds,
        )

    async def _send_pending_catalog_receipts(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        ingress: ConnectorSessionCatalogIngress,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        while True:
            async with asyncio.timeout(self._settings.router_timeout_seconds):
                catalog_message_id = await ingress.next_pending_receipt(
                    identity=identity,
                    connection_id=connection_id,
                )
            if catalog_message_id is None:
                await self._sleep(0.25)
                continue
            async with cursor_lock:
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    delivery = await ingress.reserve_pending_receipt_and_advance(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        catalog_message_id=catalog_message_id,
                        expected_next_connector_sequence=(
                            cursors.next_connector_sequence
                        ),
                        expected_next_cloud_sequence=cursors.next_cloud_sequence,
                    )
                await self._send_catalog_receipt(
                    connection,
                    identity=identity,
                    delivery=delivery,
                )
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await ingress.mark_receipt_sent(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        runtime_generation=runtime_generation,
                        catalog_message_id=delivery.catalog_message_id,
                        message_id=delivery.message_id,
                        receipt_sequence=delivery.sequence,
                    )
                cursors.next_cloud_sequence += 1

    async def _send_pending_observer_receipts(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        observer_v2_active: bool,
        router: ConnectorObserverReceiptRouter,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        while True:
            async with asyncio.timeout(self._settings.router_timeout_seconds):
                observer_message_id = await router.next_pending(
                    identity=identity,
                    connection_id=connection_id,
                )
            if observer_message_id is None:
                await self._sleep(0.25)
                continue
            async with cursor_lock:
                sequence = cursors.next_cloud_sequence
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    delivery = await router.reserve_redelivery(
                        identity=identity,
                        connection_id=connection_id,
                        observer_message_id=observer_message_id,
                        sequence=sequence,
                    )
                receipt = self._observer_receipt_envelope(
                    identity,
                    delivery,
                    observer_v2_active=observer_v2_active,
                )
                await connection.send_text(
                    self._codec.encode_connector_frame(receipt),
                    timeout_seconds=self._settings.io_timeout_seconds,
                )
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    await router.mark_sent(
                        identity=identity,
                        connection_id=connection_id,
                        observer_message_id=delivery.observer_message_id,
                        message_id=delivery.message_id,
                        sequence=delivery.sequence,
                    )
                await self._commit_cursor_advance(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    cursors=cursors,
                    next_connector_sequence=cursors.next_connector_sequence,
                    next_cloud_sequence=cursors.next_cloud_sequence + 1,
                )

    def _observer_receipt_envelope(
        self,
        identity: ConnectorIdentity,
        delivery: ConnectorObserverReceiptDelivery,
        *,
        observer_v2_active: bool,
    ) -> CloudEnvelope:
        stored_v2 = delivery.payload.get("observer_contract") == 2
        if stored_v2 != observer_v2_active:
            raise ConnectorSessionError(
                "pending observer receipt contract does not match the session"
            )
        message_type = (
            f"{delivery.message_type}.v2"
            if observer_v2_active
            else delivery.message_type
        )
        if observer_v2_active:
            require_payload(message_type.replace(".", "-"), delivery.payload)
        return CloudEnvelope(
            contract_version=1,
            message_id=delivery.message_id,
            message_type=message_type,
            tenant_id=identity.tenant_id,
            device_id=identity.device_id,
            sequence=delivery.sequence,
            sent_at=delivery.sent_at,
            idempotency_key=delivery.observer_message_id,
            payload=delivery.payload,
        )

    async def _send_observer_subscriptions(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        observer_v2_active: bool,
        router: ConnectorObserverSubscriptionRouter,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        while True:
            delivery = await router.wait_for_subscription_intent(
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            )
            if delivery is None:
                continue
            if (
                delivery.message_type
                not in {"session.observe.open", "session.observe.close"}
                or delivery.message_id != delivery.request_id
                or delivery.payload.get("request_id") != delivery.request_id
            ):
                raise ConnectorSessionError(
                    "routed Observer subscription intent is invalid"
                )
            async with cursor_lock:
                sequence = cursors.next_cloud_sequence
                observer_contract = 2 if observer_v2_active else 1
                wire_message_type = (
                    f"{delivery.message_type}.v2"
                    if observer_v2_active
                    else delivery.message_type
                )
                wire_payload = {
                    **dict(delivery.payload),
                    **({"observer_contract": 2} if observer_v2_active else {}),
                }
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    reserved = await router.reserve_subscription_intent(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        request_id=delivery.request_id,
                        message_id=delivery.message_id,
                        sequence=sequence,
                        observer_contract=observer_contract,
                        wire_message_type=wire_message_type,
                        wire_payload_digest=canonical_payload_digest(wire_payload),
                    )
                if (
                    reserved.message_id != reserved.request_id
                    or reserved.message_type != delivery.message_type
                    or reserved.payload.get("request_id") != reserved.request_id
                    or any(
                        reserved.payload.get(field) != delivery.payload.get(field)
                        for field in (
                            "subscription_id",
                            "profile",
                            "session_key",
                            "target_source",
                        )
                    )
                ):
                    raise ConnectorSessionError(
                        "Observer subscription reservation changed"
                    )
                reserved_wire_payload = {
                    **dict(reserved.payload),
                    **({"observer_contract": 2} if observer_v2_active else {}),
                }
                if (
                    reserved.observer_contract != observer_contract
                    or reserved.wire_message_type
                    != (
                        f"{reserved.message_type}.v2"
                        if observer_v2_active
                        else reserved.message_type
                    )
                    or reserved.wire_payload_digest
                    != canonical_payload_digest(reserved_wire_payload)
                ):
                    raise ConnectorSessionError(
                        "Observer subscription wire contract changed"
                    )
                envelope = CloudEnvelope(
                    contract_version=1,
                    message_id=reserved.message_id,
                    message_type=reserved.wire_message_type,
                    tenant_id=identity.tenant_id,
                    device_id=identity.device_id,
                    sequence=sequence,
                    sent_at=reserved.sent_at,
                    idempotency_key=reserved.request_id,
                    payload=reserved_wire_payload,
                )
                if observer_v2_active:
                    require_payload(
                        envelope.message_type.replace(".", "-"),
                        envelope.payload,
                    )
                await connection.send_text(
                    self._codec.encode_connector_frame(envelope),
                    timeout_seconds=self._settings.io_timeout_seconds,
                )
                await self._commit_cursor_advance(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    cursors=cursors,
                    next_connector_sequence=cursors.next_connector_sequence,
                    next_cloud_sequence=cursors.next_cloud_sequence + 1,
                )

    async def _send_commands(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        command_router: ConnectorCommandRouter,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        while True:
            delivery = await command_router.wait_for_delivery(
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            )
            if delivery is None:
                continue
            if (
                delivery.payload.get("command_id") != delivery.command_id
                or delivery.payload.get("connector_instance_id")
                != connector_instance_id
            ):
                raise ConnectorSessionError(
                    "routed command does not match Connector binding"
                )
            async with cursor_lock:
                sequence = cursors.next_cloud_sequence
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    reserved = await command_router.reserve_delivery(
                        identity=identity,
                        connection_id=connection_id,
                        connector_instance_id=connector_instance_id,
                        command_id=delivery.command_id,
                        message_id=delivery.message_id,
                        sequence=sequence,
                    )
                if (
                    reserved.command_id != delivery.command_id
                    or reserved.message_id != delivery.message_id
                    or reserved.payload.get("connector_instance_id")
                    != connector_instance_id
                ):
                    raise ConnectorSessionError(
                        "dispatch reservation does not match routed command"
                    )
                envelope = CloudEnvelope(
                    contract_version=1,
                    message_id=reserved.message_id,
                    message_type="command.deliver",
                    tenant_id=identity.tenant_id,
                    device_id=identity.device_id,
                    sequence=sequence,
                    sent_at=reserved.sent_at,
                    payload=dict(reserved.payload),
                )
                await connection.send_text(
                    self._codec.encode_connector_frame(envelope),
                    timeout_seconds=self._settings.io_timeout_seconds,
                )
                await self._commit_cursor_advance(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    cursors=cursors,
                    next_connector_sequence=cursors.next_connector_sequence,
                    next_cloud_sequence=cursors.next_cloud_sequence + 1,
                )

    async def _send_owner_control(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        owner_control_router: ConnectorOwnerControlRouter,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        while True:
            payload = await owner_control_router.wait_for_control_request(
                identity=identity,
                connection_id=connection_id,
                connector_instance_id=connector_instance_id,
                runtime_generation=runtime_generation,
            )
            if payload is None:
                return
            request_id = payload.get("request_id")
            if not isinstance(request_id, str):
                raise ConnectorSessionError("routed owner-control request is invalid")
            async with cursor_lock:
                sequence = cursors.next_cloud_sequence
                envelope = CloudEnvelope(
                    contract_version=1,
                    message_id=str(self._uuid_factory()),
                    message_type="control.request",
                    tenant_id=identity.tenant_id,
                    device_id=identity.device_id,
                    sequence=sequence,
                    sent_at=self._timestamp(),
                    idempotency_key=request_id,
                    payload=dict(payload),
                )
                async with asyncio.timeout(self._settings.router_timeout_seconds):
                    effect_started = (
                        await owner_control_router.control_request_effect_started(
                            identity=identity,
                            connection_id=connection_id,
                            request_id=request_id,
                        )
                    )
                if not effect_started:
                    raise ConnectorSessionError("routed owner-control request is stale")
                await connection.send_text(
                    self._codec.encode_connector_frame(envelope),
                    timeout_seconds=self._settings.io_timeout_seconds,
                )
                await self._commit_cursor_advance(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    cursors=cursors,
                    next_connector_sequence=cursors.next_connector_sequence,
                    next_cloud_sequence=cursors.next_cloud_sequence + 1,
                )

    async def _send_heartbeats(
        self,
        connection: ConnectorConnection,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        session_state: str,
        cursors: _SessionCursors,
        cursor_lock: asyncio.Lock,
    ) -> None:
        delay_seconds = self._settings.heartbeat_interval_ms / 1000
        while True:
            await self._sleep(delay_seconds)
            await self._revalidate(identity)
            async with cursor_lock:
                sequence = cursors.next_cloud_sequence
                heartbeat = CloudEnvelope(
                    contract_version=1,
                    message_id=str(self._uuid_factory()),
                    message_type="connector.heartbeat",
                    tenant_id=identity.tenant_id,
                    device_id=identity.device_id,
                    sequence=sequence,
                    sent_at=self._timestamp(),
                    payload={
                        "connection_id": connection_id,
                        "sender_role": "cloud",
                        "observed_at": self._timestamp(),
                        "next_outbound_sequence": sequence,
                        "next_inbound_sequence": (cursors.next_connector_sequence),
                        "session_state": session_state,
                    },
                )
                await connection.send_text(
                    self._codec.encode_connector_frame(heartbeat),
                    timeout_seconds=self._settings.io_timeout_seconds,
                )
                await self._commit_cursor_advance(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    cursors=cursors,
                    next_connector_sequence=cursors.next_connector_sequence,
                    next_cloud_sequence=cursors.next_cloud_sequence + 1,
                )

    async def _resolve_resume(
        self,
        identity: ConnectorIdentity,
        position,
        *,
        connector_instance_id: str,
        runtime_generation: str,
    ) -> ConnectorResumeResolution:
        if self._resume_resolver is None:
            if (
                position.mode == "fresh"
                and position.next_outbound_sequence == 0
                and position.next_inbound_sequence == 0
            ):
                return ConnectorResumeResolution("fresh", 0, 0, "advance")
            return ConnectorResumeResolution("fresh", 0, 0, "preserve")
        try:
            async with asyncio.timeout(self._settings.resume_timeout_seconds):
                resolution = await self._resume_resolver.resolve(
                    identity,
                    position,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                )
            decision = resolution.decision
            next_connector_sequence = resolution.next_connector_sequence
            next_cloud_sequence = resolution.next_cloud_sequence
            handshake_disposition = resolution.handshake_disposition
            allowed_decisions = (
                {"fresh", "reset_required"}
                if position.mode == "fresh"
                else {"fresh", "resumed", "reset_required"}
            )
            if type(decision) is not str or decision not in allowed_decisions:
                raise ConnectorSessionError("Connector resume authority is invalid")
            if (
                type(next_connector_sequence) is not int
                or type(next_cloud_sequence) is not int
                or next_connector_sequence < 0
                or next_cloud_sequence < 0
            ):
                raise ConnectorSessionError("Connector resume authority is invalid")
            if decision == "fresh" and (
                next_connector_sequence != 0 or next_cloud_sequence != 0
            ):
                raise ConnectorSessionError("Connector resume authority is invalid")
            if type(handshake_disposition) is not str or handshake_disposition not in {
                "advance",
                "preserve",
            }:
                raise ConnectorSessionError("Connector resume authority is invalid")
            initial_fresh = (
                position.mode == "fresh"
                and position.next_outbound_sequence == 0
                and position.next_inbound_sequence == 0
            )
            expected_fresh_disposition = "advance" if initial_fresh else "preserve"
            if (
                decision == "resumed"
                and handshake_disposition != "advance"
                or decision == "reset_required"
                and handshake_disposition != "preserve"
                or decision == "fresh"
                and handshake_disposition != expected_fresh_disposition
            ):
                raise ConnectorSessionError("Connector resume authority is invalid")
        except asyncio.CancelledError:
            raise
        except ConnectorSessionError:
            raise
        except Exception:  # noqa: BLE001 - injected authority fails closed
            raise ConnectorSessionError(
                "Connector resume authority is unavailable"
            ) from None
        return ConnectorResumeResolution(
            decision,
            next_connector_sequence,
            next_cloud_sequence,
            handshake_disposition,
        )

    async def _commit_cursor_advance(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        connector_instance_id: str,
        runtime_generation: str,
        cursors: _SessionCursors,
        next_connector_sequence: int,
        next_cloud_sequence: int,
    ) -> None:
        if self._transport_cursor_authority is not None:
            async with asyncio.timeout(self._settings.router_timeout_seconds):
                await self._transport_cursor_authority.commit_cursors(
                    identity=identity,
                    connection_id=connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation=runtime_generation,
                    expected_next_connector_sequence=(cursors.next_connector_sequence),
                    expected_next_cloud_sequence=cursors.next_cloud_sequence,
                    next_connector_sequence=next_connector_sequence,
                    next_cloud_sequence=next_cloud_sequence,
                )
        cursors.next_connector_sequence = next_connector_sequence
        cursors.next_cloud_sequence = next_cloud_sequence

    def _negotiate(
        self,
        required: Sequence[str],
        optional: Sequence[str],
        *,
        identity: ConnectorIdentity,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        available = set(self._settings.available_capabilities)
        authorized = (
            {
                "session.catalog.v1",
                "session.observe",
                "session.observe.output-parity.v1",
            }
            if "session.observe" in identity.scopes
            else set()
        )
        if "session.control.request" in identity.scopes:
            authorized.add("session.control")
        available.intersection_update(authorized)
        if any(capability not in available for capability in required):
            raise ConnectorCapabilityUnavailable()
        accepted = tuple(
            capability
            for capability in (*required, *optional)
            if capability in available
        )
        unavailable = tuple(
            capability for capability in optional if capability not in available
        )
        return accepted, unavailable

    @staticmethod
    def _identity(authenticated: object) -> ConnectorIdentity:
        tenant_id = getattr(authenticated, "tenant_id", None)
        device_id = getattr(authenticated, "device_id", None)
        if (
            not isinstance(tenant_id, str)
            or not 1 <= len(tenant_id) <= 128
            or not isinstance(device_id, str)
            or not 1 <= len(device_id) <= 128
        ):
            raise PermissionError("invalid connector authentication result")
        if isinstance(authenticated, ConnectorIdentity):
            return authenticated
        return ConnectorIdentity(
            tenant_id,
            device_id,
            scopes=("session.observe", "session.control.request"),
        )

    async def _revalidate(self, identity: ConnectorIdentity) -> None:
        revalidate = getattr(self._authenticator, "revalidate", None)
        if not callable(revalidate):
            return
        try:
            async with asyncio.timeout(self._settings.router_timeout_seconds):
                await revalidate(identity)
        except asyncio.CancelledError:
            raise
        except (
            ConnectorAuthorizationRevoked,
            ConnectorAuthorizationSuspended,
            ConnectorAuthorizationUnavailable,
            ConnectorAuthenticationExpired,
        ):
            raise
        except Exception:  # noqa: BLE001 - authoritative recheck fails closed
            raise ConnectorAuthorizationUnavailable() from None

    @staticmethod
    def _bind_identity(
        envelope: CloudEnvelope,
        identity: ConnectorIdentity,
    ) -> None:
        if (
            envelope.tenant_id != identity.tenant_id
            or envelope.device_id != identity.device_id
        ):
            raise ConnectorIdentityMismatch(
                "connector envelope identity does not match authentication"
            )

    def _timestamp(self) -> str:
        return (
            self._utc_now()
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    async def _close(
        self,
        connection: ConnectorConnection,
        code: int,
        reason: str,
    ) -> None:
        try:
            await connection.close(
                code=code,
                reason=reason,
                timeout_seconds=self._settings.io_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, TimeoutError):
            return
