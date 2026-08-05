from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID, uuid4

from hermes_connector.domain.canonical_json import canonical_json_bytes
from hermes_connector.domain.cloud_protocol import (
    ConnectorHeartbeat,
    ConnectorHello,
    ResumePosition,
)
from hermes_connector.domain.cloud_session import (
    CloudSessionState,
    transition_cloud_session,
)
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.observer import SessionEvent, SessionSnapshot
from hermes_connector.domain.owner_control import (
    OwnerControlRequest,
    OwnerControlResponse,
)
from hermes_connector.domain.session_catalog import (
    SessionCatalogEvent,
    SessionCatalogSnapshotPage,
)
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    CommandOutboxRecord,
    ObserverOutboxRecord,
    OwnerControlRecord,
    SessionCatalogOutboxRecord,
    StorageError,
)
from hermes_connector.ports.cloud import (
    CloudConnectionClosed,
    CloudConnectionPort,
    CloudLifecycleTokenProviderPort,
    CloudTokenProviderPort,
    CloudTransportPort,
    ConnectorProtocolCodecPort,
)
from hermes_connector.ports.control_command import CommandLanePort
from hermes_connector.ports.local_gateway import LocalRuntimeAuthorityPort
from hermes_connector.ports.observer import (
    ObserverIntentLanePort,
    ObserverOutboundLanePort,
)
from hermes_connector.ports.owner_control import OwnerControlLanePort
from hermes_connector.ports.reliable_storage import ReliableStoragePort
from hermes_connector.ports.session_catalog import (
    SessionCatalogOutboundLanePort,
    SessionCatalogSyncPort,
)

_OUTBOUND_CURSOR = "cloud.connector.outbound"
_INBOUND_CURSOR = "cloud.connector.inbound"
_POLICY_CLOSE_LIFECYCLE_SIGNALS = {
    (1008, "device_authorization_revoked"): "revoked",
    (1008, "device_authorization_suspended"): "suspended",
}


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


class CloudSessionError(RuntimeError):
    pass


class RequiredCapabilityUnavailable(CloudSessionError):
    pass


class UnsupportedCloudMessage(CloudSessionError):
    pass


class ProtocolViolation(CloudSessionError):
    pass


class LocalRuntimeAuthorityUnavailable(CloudSessionError):
    pass


class LocalRuntimeAuthorityChanged(CloudSessionError):
    pass


class ServerSessionDirective(StrEnum):
    """Local representation pending a frozen wire mapping in root contracts."""

    DRAIN = "drain"
    REVOKED = "revoked"
    UPDATE_REQUIRED = "update_required"


@dataclass(frozen=True, slots=True)
class CloudClientConfig:
    endpoint: str
    tenant_id: str
    device_id: str
    connector_instance_id: UUID
    connector_version: str
    negotiation_timeout_seconds: float = 10.0
    io_timeout_seconds: float = 10.0
    command_outbox_batch_size: int = 2_048
    owner_control_max_in_flight: int = 64

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.negotiation_timeout_seconds)
            or self.negotiation_timeout_seconds <= 0
        ):
            raise ValueError("negotiation timeout must be finite and positive")
        if not math.isfinite(self.io_timeout_seconds) or self.io_timeout_seconds <= 0:
            raise ValueError("I/O timeout must be finite and positive")
        if (
            type(self.command_outbox_batch_size) is not int
            or self.command_outbox_batch_size <= 0
        ):
            raise ValueError("command outbox batch size must be a positive integer")
        if (
            type(self.owner_control_max_in_flight) is not int
            or self.owner_control_max_in_flight <= 0
        ):
            raise ValueError("owner control max in flight must be a positive integer")


class ExponentialBackoff:
    def __init__(
        self,
        *,
        base_seconds: float,
        maximum_seconds: float,
        jitter_ratio: float,
        random_value: Callable[[], float],
    ) -> None:
        if not 0 < base_seconds <= maximum_seconds:
            raise ValueError("backoff bounds are invalid")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter ratio is invalid")
        self._base = base_seconds
        self._maximum = maximum_seconds
        self._jitter = jitter_ratio
        self._random = random_value

    def delay(self, attempt: int) -> float:
        if type(attempt) is not int or attempt < 0:
            raise ValueError("backoff attempt must be non-negative")
        random_value = self._random()
        if not 0 <= random_value <= 1:
            raise ValueError("random value must be between zero and one")
        exponential = min(self._maximum, self._base * (2**attempt))
        multiplier = 1 - self._jitter + (2 * self._jitter * random_value)
        return min(self._maximum, exponential * multiplier)


class CloudWSSClient:
    """Connector Protocol session coordinator with an optional durable command lane."""

    name = "cloud_wss"

    def __init__(
        self,
        *,
        config: CloudClientConfig,
        transport: CloudTransportPort,
        token_provider: CloudTokenProviderPort,
        storage: ReliableStoragePort,
        codec: ConnectorProtocolCodecPort,
        runtime_authority: LocalRuntimeAuthorityPort,
        command_lane: CommandLanePort | None = None,
        owner_control_lane: OwnerControlLanePort | None = None,
        observer_outbound_lane: ObserverOutboundLanePort | None = None,
        observer_intent_lane: ObserverIntentLanePort | None = None,
        session_catalog_outbound_lane: SessionCatalogOutboundLanePort | None = None,
        session_catalog_sync: SessionCatalogSyncPort | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        message_id_factory: Callable[[], UUID] = uuid4,
        epoch_id_factory: Callable[[], UUID] = uuid4,
        backoff: ExponentialBackoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._transport = transport
        self._token_provider = token_provider
        self._storage = storage
        self._codec = codec
        self._runtime_authority_source = runtime_authority
        self._command_lane = command_lane
        self._owner_control_lane = owner_control_lane
        self._observer_outbound_lane = observer_outbound_lane
        self._observer_intent_lane = observer_intent_lane
        self._session_catalog_outbound_lane = session_catalog_outbound_lane
        self._session_catalog_sync = session_catalog_sync
        self._utc_now = utc_now
        self._message_id_factory = message_id_factory
        self._epoch_id_factory = epoch_id_factory
        self._backoff = backoff or ExponentialBackoff(
            base_seconds=1.0,
            maximum_seconds=60.0,
            jitter_ratio=0.25,
            random_value=random.random,
        )
        self._sleep = sleep
        self._state = CloudSessionState.DISCONNECTED
        self._connection: CloudConnectionPort | None = None
        self._connection_id: UUID | None = None
        self._transport_epoch_id: str | None = None
        self._started_fresh_epoch = False
        self._accepted_capabilities: frozenset[str] = frozenset()
        self._session_catalog_capability_generation = 0
        self._session_catalog_capability_enabled = False
        self._session_catalog_retire_generation = -1
        self._session_catalog_capability_changed = asyncio.Condition()
        self._runtime_authority: LocalRuntimeAuthority | None = None
        self._max_in_flight = 0
        self._send_window = asyncio.Semaphore(1)
        self._outbound_sequence_lock = asyncio.Lock()
        self._outbound_sequence_failed = False
        self._in_flight = 0
        self._heartbeat_interval_ms = 0
        self._reconnect_allowed = True
        self._stopping = False
        self._command_recovery_completed = False
        self._sent_command_messages: set[tuple[str, str]] = set()
        self._sent_owner_control_responses: set[tuple[str, int]] = set()
        self._owner_control_tasks: set[asyncio.Task[None]] = set()
        self._owner_control_failure: BaseException | None = None

    @property
    def state(self) -> CloudSessionState:
        return self._state

    @property
    def connection_id(self) -> UUID | None:
        return self._connection_id

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    @property
    def reconnect_allowed(self) -> bool:
        return self._reconnect_allowed

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def bind_observer_intent_lane(self, lane: ObserverIntentLanePort) -> None:
        if self._state is not CloudSessionState.DISCONNECTED:
            raise CloudSessionError("Observer intent lane must bind before start")
        if self._observer_intent_lane is not None:
            raise CloudSessionError("Observer intent lane is already bound")
        self._observer_intent_lane = lane

    def bind_session_catalog_sync(self, sync: SessionCatalogSyncPort) -> None:
        if self._state is not CloudSessionState.DISCONNECTED:
            raise CloudSessionError("session catalog sync must bind before start")
        if self._session_catalog_sync is not None:
            raise CloudSessionError("session catalog sync is already bound")
        self._session_catalog_sync = sync

    @property
    def session_catalog_enabled(self) -> bool:
        return "session.catalog.v1" in self._accepted_capabilities

    async def wait_session_catalog_capability_change(
        self,
        after_generation: int,
    ) -> tuple[int, bool, bool]:
        async with self._session_catalog_capability_changed:
            await self._session_catalog_capability_changed.wait_for(
                lambda: self._session_catalog_capability_generation
                > after_generation
            )
            return (
                self._session_catalog_capability_generation,
                self._session_catalog_capability_enabled,
                self._session_catalog_retire_generation > after_generation,
            )

    async def retire_session_catalog_pending(self) -> None:
        lane = self._session_catalog_outbound_lane
        if lane is not None:
            await lane.retire_pending()

    async def ready(self) -> bool:
        return self._state is CloudSessionState.ACTIVE

    async def start(self) -> None:
        if self._state is not CloudSessionState.DISCONNECTED:
            raise CloudSessionError("cloud session is already started")
        self._state = transition_cloud_session(
            self._state,
            CloudSessionState.CONNECTING,
        )
        try:
            self._runtime_authority = await self._read_runtime_authority()
            await self._prepare_transport_epoch()
            token = await self._token_provider.access_token()
            if not token:
                raise CloudSessionError("cloud access token is unavailable")
            self._connection = await self._transport.connect(
                self._config.endpoint,
                token=token,
            )
            self._outbound_sequence_failed = False
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.NEGOTIATING,
            )
            deadline = (
                asyncio.get_running_loop().time()
                + self._config.negotiation_timeout_seconds
            )
            await self._negotiate(deadline)
        except asyncio.CancelledError:
            await self._disconnect(code=1002, reason="negotiation_failed")
            raise
        except Exception:
            await self._disconnect(code=1002, reason="negotiation_failed")
            raise

    async def _negotiate(self, deadline: float) -> None:
        def hello_frame(checkpoint: CloudSessionCheckpoint) -> bytes:
            outbound = checkpoint.next_outbound_sequence
            previous_connection_id = (
                UUID(checkpoint.previous_connection_id)
                if checkpoint.previous_connection_id is not None
                else None
            )
            resume = ResumePosition(
                mode="resume" if previous_connection_id else "fresh",
                previous_connection_id=previous_connection_id,
                next_outbound_sequence=outbound,
                next_inbound_sequence=checkpoint.next_inbound_sequence,
            )
            hello = ConnectorHello(
                connector_instance_id=self._config.connector_instance_id,
                connector_version=self._config.connector_version,
                runtime_generation=self._require_runtime_authority().runtime_generation,
                required_capabilities=(
                    self._require_runtime_authority().required_capabilities
                ),
                optional_capabilities=(
                    self._require_runtime_authority().optional_capabilities
                ),
                resume=resume,
            )
            envelope = self._outbound_envelope(
                "connector.hello",
                self._codec.hello_payload(hello),
                sequence=outbound,
            )
            return self._codec.encode_envelope(envelope)

        checkpoint = await self._send_handshake_frame(hello_frame, deadline=deadline)
        outbound = checkpoint.next_outbound_sequence
        inbound = checkpoint.next_inbound_sequence

        welcome_envelope = self._codec.decode_envelope(
            await self._receive_frame(deadline=deadline)
        )
        self._validate_envelope_identity(welcome_envelope)
        if (
            welcome_envelope.message_type != "connector.welcome"
            or welcome_envelope.sequence != inbound
        ):
            raise ProtocolViolation("expected connector.welcome sequence")
        welcome = self._codec.decode_welcome_payload(welcome_envelope.payload)
        self._validate_capabilities(welcome)
        self._accepted_capabilities = frozenset(welcome.accepted_capabilities)
        catalog_retired = (
            "session.catalog.v1"
            in welcome.unavailable_optional_capabilities
        )
        if catalog_retired:
            await self.retire_session_catalog_pending()
        await self._announce_session_catalog_capability(
            enabled="session.catalog.v1" in self._accepted_capabilities,
            retire_pending=catalog_retired,
        )
        self._connection_id = welcome.connection_id
        self._max_in_flight = welcome.max_in_flight
        self._send_window = asyncio.Semaphore(welcome.max_in_flight)
        self._heartbeat_interval_ms = welcome.heartbeat_interval_ms

        if welcome.resume_decision == "resumed":
            if (
                self._started_fresh_epoch
                or checkpoint.previous_connection_id is None
                or welcome.next_connector_sequence != outbound + 1
                or welcome.next_cloud_sequence != inbound + 1
            ):
                raise ProtocolViolation(
                    "resumed welcome does not strictly advance the handshake pair"
                )
            await self._storage.commit_transport_handshake(
                epoch_id=self._require_transport_epoch(),
                previous_connection_id=str(welcome.connection_id),
                next_outbound_sequence=welcome.next_connector_sequence,
                next_inbound_sequence=welcome.next_cloud_sequence,
            )
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.ACTIVE,
            )
            await self._activate_business_lanes()
            return

        requires_reconciliation = (
            checkpoint.reconciliation_required
            or welcome.resume_decision != "resumed"
            or welcome.next_connector_sequence != outbound
            or welcome.next_cloud_sequence != inbound
        )
        if requires_reconciliation:
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.RECONCILING,
            )
            if welcome.resume_decision == "fresh" and not self._started_fresh_epoch:
                self._transport_epoch_id = str(self._epoch_id_factory())
                await self._storage.begin_transport_epoch(
                    epoch_id=self._transport_epoch_id,
                    runtime_generation=(
                        self._require_runtime_authority().runtime_generation
                    ),
                    previous_connection_id=str(welcome.connection_id),
                    next_outbound_sequence=welcome.next_connector_sequence,
                    next_inbound_sequence=welcome.next_cloud_sequence,
                )
            elif welcome.resume_decision == "fresh":
                await self._storage.commit_transport_handshake(
                    epoch_id=self._require_transport_epoch(),
                    previous_connection_id=str(welcome.connection_id),
                    next_outbound_sequence=welcome.next_connector_sequence,
                    next_inbound_sequence=welcome.next_cloud_sequence,
                )
            else:
                await self._storage.reconcile_transport_epoch(
                    epoch_id=self._require_transport_epoch(),
                    previous_connection_id=str(welcome.connection_id),
                    next_outbound_sequence=welcome.next_connector_sequence,
                    next_inbound_sequence=welcome.next_cloud_sequence,
                )
            await self._reconcile_pending_outbox(deadline=deadline)
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.ACTIVE,
            )
            await self._activate_business_lanes()
            return
        await self._storage.reconcile_transport_epoch(
            epoch_id=self._require_transport_epoch(),
            previous_connection_id=str(welcome.connection_id),
            next_outbound_sequence=welcome.next_connector_sequence,
            next_inbound_sequence=welcome.next_cloud_sequence,
        )
        self._state = transition_cloud_session(
            self._state,
            CloudSessionState.ACTIVE,
        )
        await self._activate_business_lanes()

    async def _prepare_transport_epoch(self) -> None:
        checkpoint = await self._storage.get_cloud_session()
        runtime_generation = self._require_runtime_authority().runtime_generation
        requires_fresh = (
            checkpoint.fresh_epoch_required
            or checkpoint.transport_epoch_id is None
            or checkpoint.runtime_generation != runtime_generation
        )
        self._started_fresh_epoch = requires_fresh
        if requires_fresh:
            self._transport_epoch_id = str(self._epoch_id_factory())
            await self._storage.begin_transport_epoch(
                epoch_id=self._transport_epoch_id,
                runtime_generation=runtime_generation,
                previous_connection_id=None,
                next_outbound_sequence=0,
                next_inbound_sequence=0,
            )
            return
        self._transport_epoch_id = checkpoint.transport_epoch_id

    async def _send_handshake_frame(
        self,
        frame_factory: Callable[[CloudSessionCheckpoint], bytes],
        *,
        deadline: float,
    ) -> CloudSessionCheckpoint:
        await self._require_current_runtime_authority()
        async with asyncio.timeout(self._remaining(deadline)):
            await self._outbound_sequence_lock.acquire()
        try:
            checkpoint = await self._storage.get_cloud_session()
            frame = frame_factory(checkpoint)
            remaining = self._remaining(deadline)
            async with asyncio.timeout(remaining):
                await self._require_connection().send(
                    frame,
                    timeout_seconds=remaining,
                )
            await self._require_current_runtime_authority()
            return checkpoint
        except BaseException:
            self._outbound_sequence_failed = True
            raise
        finally:
            self._outbound_sequence_lock.release()

    async def receive_one(self) -> None:
        if self._observer_intent_lane is not None:
            self._observer_intent_lane.raise_if_failed()
        if self._owner_control_failure is not None:
            failure = self._owner_control_failure
            self._owner_control_failure = None
            raise failure
        checkpoint = await self._storage.get_cloud_session()
        envelope = self._codec.decode_envelope(await self._receive_frame())
        self._validate_envelope_identity(envelope)
        if envelope.message_type in {
            "session.observe.open",
            "session.observe.close",
            "stream.ack",
            "stream.nack",
            "session.observe.open.v2",
            "session.observe.close.v2",
            "stream.ack.v2",
            "stream.nack.v2",
        }:
            await self._process_observer_inbound(envelope, checkpoint)
            return
        if envelope.message_type in {
            "session.catalog.ack",
            "session.catalog.nack",
        }:
            await self._process_session_catalog_inbound(envelope, checkpoint)
            return
        if envelope.message_type == "control.request":
            if (
                self._owner_control_lane is None
                or "session.control" not in self._accepted_capabilities
            ):
                await self._disconnect(code=1003, reason="unsupported_message")
                raise UnsupportedCloudMessage("owner-control handling is not enabled")
            if envelope.sequence != checkpoint.next_inbound_sequence:
                await self._disconnect(
                    code=1002,
                    reason="invalid_control_sequence",
                )
                raise ProtocolViolation("control request sequence is invalid")
            request = self._codec.decode_control_request_payload(envelope.payload)
            if envelope.idempotency_key != str(request.request_id):
                await self._disconnect(
                    code=1002,
                    reason="invalid_control_request",
                )
                raise ProtocolViolation("control request idempotency key is invalid")
            request_payload = canonical_json_bytes(_plain_json(envelope.payload))
            scope_payload = canonical_json_bytes(
                {
                    "control_transport_id": str(request.control_transport_id),
                    "device_id": envelope.device_id,
                    "tenant_id": envelope.tenant_id,
                }
            )
            await self._storage.put_owner_control_and_advance_inbound(
                expected_sequence=envelope.sequence,
                request_id=str(request.request_id),
                request_digest=hashlib.sha256(request_payload).hexdigest(),
                control_transport_id=str(request.control_transport_id),
                operation=request.operation,
                request_payload=request_payload,
                scope_payload=scope_payload,
            )
            await self._schedule_owner_control(request)
            return
        if envelope.message_type == "command.deliver":
            if (
                self._command_lane is None
                or "session.control" not in self._accepted_capabilities
            ):
                await self._disconnect(code=1003, reason="unsupported_message")
                raise UnsupportedCloudMessage(
                    "business message handling is not enabled"
                )
            if envelope.sequence != checkpoint.next_inbound_sequence:
                await self._disconnect(code=1002, reason="invalid_command_sequence")
                raise ProtocolViolation("command delivery sequence is invalid")
            await self._command_lane.process(envelope)
            await self._storage.advance_cloud_inbound(envelope.sequence)
            await self._flush_command_outbox()
            return
        if envelope.message_type != "connector.heartbeat":
            await self._disconnect(code=1003, reason="unsupported_message")
            raise UnsupportedCloudMessage("business message handling is not enabled")

        heartbeat = self._codec.decode_heartbeat_payload(envelope.payload)
        if (
            heartbeat.sender_role != "cloud"
            or heartbeat.connection_id != self._connection_id
        ):
            await self._disconnect(code=1002, reason="invalid_heartbeat")
            raise ProtocolViolation("cloud heartbeat identity is invalid")

        outbound = checkpoint.next_outbound_sequence
        inbound = checkpoint.next_inbound_sequence
        sequence_gap = envelope.sequence != inbound
        cursor_gap = (
            heartbeat.next_outbound_sequence != inbound
            or heartbeat.next_inbound_sequence != outbound
        )
        if sequence_gap or cursor_gap:
            self._sent_command_messages.clear()
            if self._state is CloudSessionState.ACTIVE:
                self._state = transition_cloud_session(
                    self._state,
                    CloudSessionState.RECONCILING,
                )
            await self._storage.reconcile_transport_epoch(
                epoch_id=self._require_transport_epoch(),
                previous_connection_id=str(self._connection_id),
                next_outbound_sequence=heartbeat.next_inbound_sequence,
                next_inbound_sequence=heartbeat.next_outbound_sequence,
            )
            await self._reconcile_pending_outbox()
            if self._state is CloudSessionState.RECONCILING:
                self._state = transition_cloud_session(
                    self._state,
                    CloudSessionState.ACTIVE,
                )
            await self._flush_command_outbox()
            return
        await self._storage.settle_transport_cursor(
            epoch_id=self._require_transport_epoch(),
            next_sequence=heartbeat.next_inbound_sequence,
        )
        await self._storage.advance_cloud_inbound(inbound)
        await self._flush_command_outbox()

    async def publish_observer_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        force_new_attempt: bool = False,
    ) -> ObserverOutboxRecord:
        return await self._publish_observer_fact(
            snapshot=snapshot,
            force_new_attempt=force_new_attempt,
        )

    async def publish_observer_event(
        self,
        event: SessionEvent,
    ) -> ObserverOutboxRecord:
        return await self._publish_observer_fact(event=event)

    async def publish_session_catalog_snapshot_page(
        self,
        page: SessionCatalogSnapshotPage,
        *,
        force_new_attempt: bool = False,
    ) -> SessionCatalogOutboxRecord:
        return await self._publish_session_catalog_fact(
            page=page,
            force_new_attempt=force_new_attempt,
        )

    async def publish_session_catalog_event(
        self,
        event: SessionCatalogEvent,
        *,
        force_new_attempt: bool = False,
    ) -> SessionCatalogOutboxRecord:
        return await self._publish_session_catalog_fact(
            event=event,
            force_new_attempt=force_new_attempt,
        )

    async def send_heartbeat(self) -> None:
        if self._observer_intent_lane is not None:
            self._observer_intent_lane.raise_if_failed()
        if self._state not in {
            CloudSessionState.ACTIVE,
            CloudSessionState.RECONCILING,
            CloudSessionState.DRAINING,
        }:
            raise CloudSessionError("cloud heartbeat requires an established session")
        connection_id = self._connection_id
        if connection_id is None:
            raise CloudSessionError("cloud connection id is unavailable")

        def heartbeat_frame(checkpoint: CloudSessionCheckpoint) -> bytes:
            outbound = checkpoint.next_outbound_sequence
            heartbeat = ConnectorHeartbeat(
                connection_id=connection_id,
                sender_role="connector",
                observed_at=self._utc_now(),
                next_outbound_sequence=outbound,
                next_inbound_sequence=checkpoint.next_inbound_sequence,
                session_state=self._state.value,
            )
            envelope = self._outbound_envelope(
                "connector.heartbeat",
                self._codec.heartbeat_payload(heartbeat),
                sequence=outbound,
            )
            return self._codec.encode_envelope(envelope)

        checkpoint = await self._storage.get_cloud_session()
        await self._send_durable_frame(
            heartbeat_frame,
            business_kind="heartbeat",
            business_key=f"heartbeat-{checkpoint.next_outbound_sequence}",
            business_revision=checkpoint.next_outbound_sequence,
        )

    async def run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                await self._run_connected()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (
                CloudSessionError,
                ConnectionError,
                OSError,
                StorageError,
                TimeoutError,
                TypeError,
                ValueError,
            ):
                await self._disconnect(code=1011, reason="transport_lost")
                if self._stopping or not self._reconnect_allowed:
                    return
                await self._sleep(self._backoff.delay(attempt))
                attempt += 1
                if self._stopping:
                    return
                try:
                    await self.start()
                except asyncio.CancelledError:
                    raise
                except (
                    CloudSessionError,
                    ConnectionError,
                    OSError,
                    StorageError,
                    TimeoutError,
                    TypeError,
                    ValueError,
                ):
                    continue
                attempt = 0

    async def _run_connected(self) -> None:
        receiver = asyncio.create_task(self._receive_loop())
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        tasks = {receiver, heartbeat}
        try:
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive_loop(self) -> None:
        while not self._stopping and self._connection is not None:
            await self.receive_one()

    async def _heartbeat_loop(self) -> None:
        while not self._stopping and self._connection is not None:
            await asyncio.sleep(self._heartbeat_interval_ms / 1000)
            if not self._stopping and self._connection is not None:
                await self.send_heartbeat()

    async def apply_server_directive(
        self,
        directive: ServerSessionDirective,
    ) -> None:
        if directive is ServerSessionDirective.DRAIN:
            if self._state in {
                CloudSessionState.ACTIVE,
                CloudSessionState.RECONCILING,
            }:
                self._state = transition_cloud_session(
                    self._state,
                    CloudSessionState.DRAINING,
                )
            return
        self._reconnect_allowed = False
        try:
            if directive is ServerSessionDirective.REVOKED and isinstance(
                self._token_provider,
                CloudLifecycleTokenProviderPort,
            ):
                await self._token_provider.apply_lifecycle_signal("revoked")
            else:
                await self._token_provider.clear_access_token()
        finally:
            await self._disconnect(code=1000, reason="")

    async def drain(self) -> None:
        if self._state in {
            CloudSessionState.ACTIVE,
            CloudSessionState.RECONCILING,
        }:
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.DRAINING,
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._observer_intent_lane is not None:
            await self._observer_intent_lane.shutdown()
        if self._state is CloudSessionState.DISCONNECTED:
            return
        if self._state in {
            CloudSessionState.ACTIVE,
            CloudSessionState.RECONCILING,
        }:
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.DRAINING,
            )
        await self._disconnect(code=1000, reason="connector_stopped")

    async def _disconnect(self, *, code: int, reason: str) -> None:
        connection = self._connection
        self._connection = None
        self._accepted_capabilities = frozenset()
        await self._announce_session_catalog_capability(
            enabled=False,
            retire_pending=False,
        )
        self._runtime_authority = None
        self._sent_command_messages.clear()
        self._sent_owner_control_responses.clear()
        self._command_recovery_completed = False
        await self._close_owner_control()
        if connection is not None:
            with suppress(ConnectionError, OSError, TimeoutError):
                await connection.close(
                    code=code,
                    reason=reason,
                    timeout_seconds=self._config.io_timeout_seconds,
                )
        if self._state is not CloudSessionState.DISCONNECTED:
            self._state = transition_cloud_session(
                self._state,
                CloudSessionState.DISCONNECTED,
            )

    async def _announce_session_catalog_capability(
        self,
        *,
        enabled: bool,
        retire_pending: bool,
    ) -> None:
        async with self._session_catalog_capability_changed:
            self._session_catalog_capability_generation += 1
            self._session_catalog_capability_enabled = enabled
            if retire_pending:
                self._session_catalog_retire_generation = (
                    self._session_catalog_capability_generation
                )
            self._session_catalog_capability_changed.notify_all()

    def _outbound_envelope(
        self,
        message_type: str,
        payload: Mapping[str, object],
        *,
        sequence: int,
        idempotency_key: str | None = None,
    ) -> CloudEnvelope:
        return CloudEnvelope(
            contract_version=1,
            message_id=self._message_id_factory(),
            message_type=message_type,
            tenant_id=self._config.tenant_id,
            device_id=self._config.device_id,
            sequence=sequence,
            sent_at=self._utc_now(),
            payload=MappingProxyType(dict(payload)),
            idempotency_key=idempotency_key,
        )

    def _validate_envelope_identity(self, envelope: CloudEnvelope) -> None:
        if (
            envelope.tenant_id != self._config.tenant_id
            or envelope.device_id != self._config.device_id
        ):
            raise ProtocolViolation("cloud envelope binding does not match")

    def _validate_capabilities(self, welcome) -> None:
        accepted = set(welcome.accepted_capabilities)
        unavailable = set(welcome.unavailable_optional_capabilities)
        authority = self._require_runtime_authority()
        required = set(authority.required_capabilities)
        optional = set(authority.optional_capabilities)
        if not required.issubset(accepted):
            raise RequiredCapabilityUnavailable(
                "cloud did not accept every required capability"
            )
        if accepted - required - optional or unavailable - optional:
            raise ProtocolViolation("cloud returned an unrequested capability")
        if accepted.intersection(unavailable):
            raise ProtocolViolation("cloud capability sets overlap")
        if optional - accepted - unavailable:
            raise ProtocolViolation("cloud omitted optional capability disposition")

    async def _reconcile_pending_outbox(
        self,
        *,
        deadline: float | None = None,
    ) -> None:
        remaining = self._remaining(deadline)
        async with asyncio.timeout(remaining):
            await self._outbound_sequence_lock.acquire()
        try:
            if self._outbound_sequence_failed:
                raise ConnectionError("cloud outbound sequence requires reconnect")
            checkpoint = await self._storage.get_cloud_session()
            epoch_id = self._require_transport_epoch()
            after_sequence = (
                checkpoint.next_outbound_sequence - 1
                if checkpoint.next_outbound_sequence > 0
                else None
            )
            while True:
                records = await self._storage.pending_transport_frames(
                    epoch_id=epoch_id,
                    limit=self._max_in_flight,
                    after_sequence=after_sequence,
                )
                if not records:
                    return
                record = records[0]
                checkpoint = await self._storage.get_cloud_session()
                if record.sequence != checkpoint.next_outbound_sequence:
                    raise ProtocolViolation(
                        "durable transport journal has a sequence gap"
                    )
                await self._transmit_journaled_frame(
                    record.frame,
                    epoch_id=epoch_id,
                    sequence=record.sequence,
                    deadline=deadline,
                )
                after_sequence = record.sequence
        except BaseException:
            self._outbound_sequence_failed = True
            raise
        finally:
            self._outbound_sequence_lock.release()

    async def _process_observer_inbound(
        self,
        envelope: CloudEnvelope,
        checkpoint: CloudSessionCheckpoint,
    ) -> None:
        if "session.observe" not in self._accepted_capabilities:
            await self._disconnect(code=1003, reason="unsupported_message")
            raise UnsupportedCloudMessage("Observer handling is not negotiated")
        is_v2 = envelope.message_type.endswith(".v2")
        if is_v2 and (
            "session.observe.output-parity.v1" not in self._accepted_capabilities
        ):
            await self._disconnect(code=1003, reason="unsupported_message")
            raise UnsupportedCloudMessage("Observer v2 handling is not negotiated")
        if envelope.sequence != checkpoint.next_inbound_sequence:
            await self._disconnect(code=1002, reason="invalid_observer_sequence")
            raise ProtocolViolation("Observer inbound sequence is invalid")

        if envelope.message_type in {"session.observe.open", "session.observe.open.v2"}:
            lane = self._observer_intent_lane
            if lane is None:
                await self._disconnect(code=1003, reason="unsupported_message")
                raise UnsupportedCloudMessage("Observer intent handling is not enabled")
            intent = (
                self._codec.decode_session_observe_open_v2_payload(envelope.payload)
                if is_v2
                else self._codec.decode_session_observe_open_payload(envelope.payload)
            )
            if envelope.idempotency_key != str(intent.request_id):
                await self._disconnect(code=1002, reason="invalid_observer_intent")
                raise ProtocolViolation("Observer open idempotency key is invalid")
            await lane.open(intent)
        elif envelope.message_type in {
            "session.observe.close",
            "session.observe.close.v2",
        }:
            lane = self._observer_intent_lane
            if lane is None:
                await self._disconnect(code=1003, reason="unsupported_message")
                raise UnsupportedCloudMessage("Observer intent handling is not enabled")
            intent = (
                self._codec.decode_session_observe_close_v2_payload(envelope.payload)
                if is_v2
                else self._codec.decode_session_observe_close_payload(envelope.payload)
            )
            if envelope.idempotency_key != str(intent.request_id):
                await self._disconnect(code=1002, reason="invalid_observer_intent")
                raise ProtocolViolation("Observer close idempotency key is invalid")
            await lane.close(intent)
        elif envelope.message_type in {"stream.ack", "stream.ack.v2"}:
            lane = self._observer_outbound_lane
            if lane is None:
                await self._disconnect(code=1003, reason="unsupported_message")
                raise UnsupportedCloudMessage("Observer ACK handling is not enabled")
            ack = (
                self._codec.decode_stream_ack_v2_payload(envelope.payload)
                if is_v2
                else self._codec.decode_stream_ack_payload(envelope.payload)
            )
            await lane.acknowledge(ack)
            if self._observer_intent_lane is not None:
                await self._observer_intent_lane.acknowledge(ack)
        else:
            lane = self._observer_outbound_lane
            if lane is None:
                await self._disconnect(code=1003, reason="unsupported_message")
                raise UnsupportedCloudMessage("Observer NACK handling is not enabled")
            nack = (
                self._codec.decode_stream_nack_v2_payload(envelope.payload)
                if is_v2
                else self._codec.decode_stream_nack_payload(envelope.payload)
            )
            await lane.reject(nack)
            if self._observer_intent_lane is not None:
                await self._observer_intent_lane.recover(nack)
        await self._storage.advance_cloud_inbound(envelope.sequence)

    async def _process_session_catalog_inbound(
        self,
        envelope: CloudEnvelope,
        checkpoint: CloudSessionCheckpoint,
    ) -> None:
        lane = self._session_catalog_outbound_lane
        if (
            lane is None
            or "session.catalog.v1" not in self._accepted_capabilities
        ):
            await self._disconnect(code=1003, reason="unsupported_message")
            raise UnsupportedCloudMessage(
                "session catalog handling is not negotiated"
            )
        if envelope.sequence != checkpoint.next_inbound_sequence:
            await self._disconnect(code=1002, reason="invalid_catalog_sequence")
            raise ProtocolViolation("session catalog inbound sequence is invalid")
        if envelope.message_type == "session.catalog.ack":
            ack = self._codec.decode_session_catalog_ack_payload(envelope.payload)
            await lane.acknowledge(ack)
            if self._session_catalog_sync is not None:
                await self._session_catalog_sync.acknowledge(ack)
        else:
            nack = self._codec.decode_session_catalog_nack_payload(envelope.payload)
            await lane.reject(nack)
            if self._session_catalog_sync is not None:
                await self._session_catalog_sync.recover(nack)
        await self._storage.advance_cloud_inbound(envelope.sequence)

    async def _publish_session_catalog_fact(
        self,
        *,
        page: SessionCatalogSnapshotPage | None = None,
        event: SessionCatalogEvent | None = None,
        force_new_attempt: bool = False,
    ) -> SessionCatalogOutboxRecord:
        lane = self._session_catalog_outbound_lane
        if lane is None or "session.catalog.v1" not in self._accepted_capabilities:
            raise UnsupportedCloudMessage(
                "session catalog outbound lane is not negotiated"
            )
        if (page is None) == (event is None):
            raise ValueError("exactly one session catalog fact is required")
        await self._require_current_runtime_authority()
        authority = self._require_runtime_authority()
        fact_profile = page.profile if page is not None else event.profile  # type: ignore[union-attr]
        fact_generation = (
            page.runtime_generation
            if page is not None
            else event.runtime_generation  # type: ignore[union-attr]
        )
        if (
            fact_profile != authority.profile
            or fact_generation != authority.runtime_generation
        ):
            raise LocalRuntimeAuthorityChanged(
                "session catalog fact does not match runtime authority"
            )
        async with asyncio.timeout(self._config.io_timeout_seconds):
            await self._outbound_sequence_lock.acquire()
        try:
            if self._outbound_sequence_failed:
                raise ConnectionError("cloud outbound sequence requires reconnect")
            checkpoint = await self._storage.get_cloud_session()
            if page is not None:
                record = await lane.stage_snapshot_page(
                    page,
                    connector_sequence=checkpoint.next_outbound_sequence,
                    force_new_attempt=force_new_attempt,
                    transport_epoch_id=self._require_transport_epoch(),
                )
            else:
                assert event is not None
                record = await lane.stage_event(
                    event,
                    connector_sequence=checkpoint.next_outbound_sequence,
                    force_new_attempt=force_new_attempt,
                    transport_epoch_id=self._require_transport_epoch(),
                )
            if record.connector_sequence < checkpoint.next_outbound_sequence:
                return record
            if record.connector_sequence > checkpoint.next_outbound_sequence:
                raise ProtocolViolation("durable session catalog sequence has a gap")
            await self._transmit_journaled_frame(
                record.frame,
                epoch_id=self._require_transport_epoch(),
                sequence=record.connector_sequence,
            )
            await lane.transport_sent(record)
            return record
        except BaseException:
            self._outbound_sequence_failed = True
            raise
        finally:
            self._outbound_sequence_lock.release()

    async def _publish_observer_fact(
        self,
        *,
        snapshot: SessionSnapshot | None = None,
        event: SessionEvent | None = None,
        force_new_attempt: bool = False,
    ) -> ObserverOutboxRecord:
        lane = self._observer_outbound_lane
        if lane is None:
            raise UnsupportedCloudMessage("Observer outbound lane is not enabled")
        if (snapshot is None) == (event is None):
            raise ValueError("exactly one Observer fact is required")
        await self._require_current_runtime_authority()
        async with asyncio.timeout(self._config.io_timeout_seconds):
            await self._outbound_sequence_lock.acquire()
        try:
            if self._outbound_sequence_failed:
                raise ConnectionError("cloud outbound sequence requires reconnect")
            checkpoint = await self._storage.get_cloud_session()
            if snapshot is not None:
                record = await lane.stage_snapshot(
                    snapshot,
                    connector_sequence=checkpoint.next_outbound_sequence,
                    force_new_attempt=force_new_attempt,
                    transport_epoch_id=self._require_transport_epoch(),
                )
            else:
                assert event is not None
                record = await lane.stage_event(
                    event,
                    connector_sequence=checkpoint.next_outbound_sequence,
                    transport_epoch_id=self._require_transport_epoch(),
                )
            if record.connector_sequence < checkpoint.next_outbound_sequence:
                return record
            if record.connector_sequence > checkpoint.next_outbound_sequence:
                raise ProtocolViolation("durable Observer sequence has a gap")
            await self._transmit_journaled_frame(
                record.frame,
                epoch_id=self._require_transport_epoch(),
                sequence=record.connector_sequence,
            )
            await lane.transport_sent(record)
            return record
        except BaseException:
            self._outbound_sequence_failed = True
            raise
        finally:
            self._outbound_sequence_lock.release()

    async def _activate_command_lane(self) -> None:
        if self._command_lane is None:
            return
        if not self._command_recovery_completed:
            while True:
                recovered = await self._command_lane.recover_inflight(
                    limit=self._max_in_flight,
                )
                await self._flush_command_outbox()
                if recovered == 0:
                    break
                await self._require_current_runtime_authority()
            self._command_recovery_completed = True
            return
        await self._flush_command_outbox()

    async def _activate_business_lanes(self) -> None:
        await self._activate_command_lane()
        await self._activate_owner_control_lane()

    async def _activate_owner_control_lane(self) -> None:
        if self._owner_control_lane is None:
            return
        await self._flush_owner_control_outbox()
        while True:
            received = await self._storage.owner_control_records(
                state="received",
                limit=self._config.owner_control_max_in_flight,
            )
            if not received:
                return
            batch = tuple(
                [
                    await self._schedule_owner_control(
                        self._owner_request(record),
                        flush_response=False,
                    )
                    for record in received
                ]
            )
            try:
                await asyncio.gather(*batch)
            except BaseException:
                for task in batch:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*batch, return_exceptions=True)
                raise
            await self._require_current_runtime_authority()
            await self._flush_owner_control_outbox()

    def _owner_request(self, record: OwnerControlRecord) -> OwnerControlRequest:
        payload = json.loads(record.request_payload)
        if not isinstance(payload, dict):
            raise ProtocolViolation("durable owner request payload is invalid")
        canonical_payload = canonical_json_bytes(payload)
        expected_scope = canonical_json_bytes(
            {
                "control_transport_id": record.control_transport_id,
                "device_id": self._config.device_id,
                "tenant_id": self._config.tenant_id,
            }
        )
        if (
            canonical_payload != record.request_payload
            or hashlib.sha256(canonical_payload).hexdigest() != record.request_digest
            or record.scope_payload != expected_scope
        ):
            raise ProtocolViolation("durable owner request integrity is invalid")
        request = self._codec.decode_control_request_payload(payload)
        if (
            str(request.request_id) != record.request_id
            or str(request.control_transport_id) != record.control_transport_id
            or request.operation != record.operation
        ):
            raise ProtocolViolation("durable owner request identity is invalid")
        return request

    async def _flush_command_outbox(self) -> None:
        lane = self._command_lane
        if lane is None or self._max_in_flight <= 0:
            return
        cursor: tuple[str, str, str] | None = None
        while True:
            pending = await lane.pending_cloud_messages(
                limit=self._config.command_outbox_batch_size,
                after_created_at=cursor[0] if cursor else None,
                after_command_id=cursor[1] if cursor else None,
                after_message_type=cursor[2] if cursor else None,
            )
            if not pending:
                return
            for record in pending:
                identity = (record.command_id, record.message_type)
                if identity not in self._sent_command_messages:
                    await self._send_command_message(record)
                    self._sent_command_messages.add(identity)
            last = pending[-1]
            cursor = (last.created_at, last.command_id, last.message_type)

    async def _send_command_message(self, record: CommandOutboxRecord) -> None:
        def command_frame(checkpoint: CloudSessionCheckpoint) -> bytes:
            if record.message_type == "command.receipt":
                message = self._codec.decode_command_receipt(record.payload)
                payload = self._codec.command_receipt_payload(message)
            elif record.message_type == "command.result":
                message = self._codec.decode_command_result(record.payload)
                payload = self._codec.command_result_payload(message)
            else:
                raise ProtocolViolation("unsupported command outbox message")
            return self._codec.encode_envelope(
                self._outbound_envelope(
                    record.message_type,
                    payload,
                    sequence=checkpoint.next_outbound_sequence,
                )
            )

        await self._send_durable_frame(
            command_frame,
            business_kind=record.message_type,
            business_key=record.command_id,
            business_revision=record.revision,
        )

    async def _schedule_owner_control(
        self,
        request: OwnerControlRequest,
        *,
        flush_response: bool = True,
    ) -> asyncio.Task[None]:
        while (
            len(self._owner_control_tasks) >= self._config.owner_control_max_in_flight
        ):
            done, _pending = await asyncio.wait(
                self._owner_control_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        task = asyncio.create_task(
            self._process_owner_control(request, flush_response=flush_response)
        )
        self._owner_control_tasks.add(task)
        task.add_done_callback(self._owner_control_done)
        return task

    async def _process_owner_control(
        self,
        request: OwnerControlRequest,
        *,
        flush_response: bool,
    ) -> None:
        lane = self._owner_control_lane
        if lane is None:
            return
        request_id = str(request.request_id)
        try:
            claimed = await self._storage.claim_owner_control(request_id)
        except BaseException:
            record = await asyncio.shield(self._storage.get_owner_control(request_id))
            if record is not None and record.state == "executing":
                await asyncio.shield(
                    self._storage.mark_owner_control_effect_unknown(request_id)
                )
            raise
        if not claimed:
            if flush_response:
                await self._flush_owner_control_outbox()
            return
        try:
            response = await lane.process(request)
            response_payload = canonical_json_bytes(
                _plain_json(self._codec.control_response_payload(response))
            )
            await self._storage.complete_owner_control(
                request_id=request_id,
                response_payload=response_payload,
                response_revision=1,
            )
        except BaseException:
            await asyncio.shield(
                self._storage.mark_owner_control_effect_unknown(request_id)
            )
            raise
        if flush_response:
            await self._flush_owner_control_outbox()

    def _owner_control_done(self, task: asyncio.Task[None]) -> None:
        self._owner_control_tasks.discard(task)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None and self._owner_control_failure is None:
            self._owner_control_failure = failure

    async def _send_owner_control_response(
        self,
        response: OwnerControlResponse,
        *,
        response_revision: int = 1,
    ) -> None:
        def response_frame(checkpoint: CloudSessionCheckpoint) -> bytes:
            return self._codec.encode_envelope(
                self._outbound_envelope(
                    "control.response",
                    self._codec.control_response_payload(response),
                    sequence=checkpoint.next_outbound_sequence,
                    idempotency_key=str(response.request_id),
                )
            )

        await self._send_durable_frame(
            response_frame,
            business_kind="control.response",
            business_key=str(response.request_id),
            business_revision=response_revision,
        )

    async def _flush_owner_control_outbox(self) -> None:
        if self._owner_control_lane is None:
            return
        cursor: tuple[str, str] | None = None
        while True:
            pending = await self._storage.pending_owner_control(
                limit=self._config.owner_control_max_in_flight,
                after_created_at=cursor[0] if cursor else None,
                after_request_id=cursor[1] if cursor else None,
            )
            if not pending:
                return
            for record in pending:
                identity = (record.request_id, record.response_revision)
                if identity in self._sent_owner_control_responses:
                    continue
                if record.response_payload is None:
                    raise ProtocolViolation(
                        "terminal owner response payload is missing"
                    )
                payload = json.loads(record.response_payload)
                if not isinstance(payload, dict):
                    raise ProtocolViolation("durable owner response payload is invalid")
                response = self._codec.decode_control_response_payload(payload)
                await self._send_owner_control_response(
                    response,
                    response_revision=record.response_revision,
                )
                self._sent_owner_control_responses.add(identity)
            last = pending[-1]
            cursor = (last.created_at, last.request_id)

    async def _close_owner_control(self) -> None:
        tasks = tuple(self._owner_control_tasks)
        self._owner_control_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owner_control_failure = None
        if self._owner_control_lane is not None:
            await self._owner_control_lane.close_all()

    async def _send_durable_frame(
        self,
        frame_factory: Callable[[CloudSessionCheckpoint], bytes],
        *,
        business_kind: str,
        business_key: str,
        business_revision: int,
        expected_sequence: int | None = None,
        deadline: float | None = None,
    ) -> tuple[CloudSessionCheckpoint, bool]:
        await self._require_current_runtime_authority()
        remaining = self._remaining(deadline)
        async with asyncio.timeout(remaining):
            await self._outbound_sequence_lock.acquire()
        try:
            if self._outbound_sequence_failed:
                raise ConnectionError("cloud outbound sequence requires reconnect")
            checkpoint = await self._storage.get_cloud_session()
            sequence = checkpoint.next_outbound_sequence
            if expected_sequence is not None:
                if expected_sequence < sequence:
                    return checkpoint, False
                if expected_sequence > sequence:
                    raise ProtocolViolation("durable outbox sequence has a gap")
            frame = frame_factory(checkpoint)
            envelope = self._codec.decode_envelope(frame)
            record = await self._storage.stage_transport_frame(
                epoch_id=self._require_transport_epoch(),
                sequence=sequence,
                message_id=str(envelope.message_id),
                message_type=envelope.message_type,
                business_kind=business_kind,
                business_key=business_key,
                business_revision=business_revision,
                runtime_generation=self._require_runtime_authority().runtime_generation,
                frame=frame,
            )
            if record.sequence < sequence:
                return checkpoint, False
            if record.sequence > sequence:
                raise ProtocolViolation("durable transport journal has a sequence gap")
            await self._transmit_journaled_frame(
                record.frame,
                epoch_id=record.epoch_id,
                sequence=record.sequence,
                deadline=deadline,
            )
            return checkpoint, True
        except BaseException:
            self._outbound_sequence_failed = True
            raise
        finally:
            self._outbound_sequence_lock.release()

    async def _transmit_journaled_frame(
        self,
        frame: bytes,
        *,
        epoch_id: str,
        sequence: int,
        deadline: float | None = None,
    ) -> None:
        remaining = self._remaining(deadline)
        async with asyncio.timeout(remaining):
            await self._send_window.acquire()
        self._in_flight += 1
        try:
            remaining = self._remaining(deadline)
            async with asyncio.timeout(remaining):
                await self._require_connection().send(
                    frame,
                    timeout_seconds=remaining,
                )
            # A successful transport write is not durable success if the local
            # Hermes authority changed during that write. Leave the cursor at
            # the prior sequence so reconciliation can replay the same durable
            # identity after reconnect instead of crediting a stale runtime.
            await self._require_current_runtime_authority()
            await self._storage.mark_transport_sent(
                epoch_id=epoch_id,
                sequence=sequence,
            )
        finally:
            self._in_flight -= 1
            self._send_window.release()

    async def _receive_frame(self, *, deadline: float | None = None) -> bytes:
        await self._require_current_runtime_authority()
        remaining = self._remaining(deadline)
        try:
            async with asyncio.timeout(remaining):
                frame = await self._require_connection().receive(
                    timeout_seconds=remaining
                )
            await self._require_current_runtime_authority()
            return frame
        except CloudConnectionClosed as error:
            await self._apply_remote_close(error)
            raise

    async def _apply_remote_close(self, error: CloudConnectionClosed) -> None:
        lifecycle_signal = _POLICY_CLOSE_LIFECYCLE_SIGNALS.get(
            (error.code, error.reason)
        )
        if lifecycle_signal is None:
            await self._disconnect(code=1000, reason="")
            return
        self._reconnect_allowed = False
        try:
            if isinstance(
                self._token_provider,
                CloudLifecycleTokenProviderPort,
            ):
                await self._token_provider.apply_lifecycle_signal(lifecycle_signal)
            else:
                await self._token_provider.clear_access_token()
        finally:
            await self._disconnect(code=1000, reason="")

    def _remaining(self, deadline: float | None) -> float:
        if deadline is None:
            return self._config.io_timeout_seconds
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        return min(remaining, self._config.io_timeout_seconds)

    def _require_connection(self) -> CloudConnectionPort:
        if self._connection is None:
            raise CloudSessionError("cloud connection is unavailable")
        return self._connection

    async def _read_runtime_authority(self) -> LocalRuntimeAuthority:
        authority = await self._runtime_authority_source.current_runtime_authority()
        if authority is None:
            raise LocalRuntimeAuthorityUnavailable(
                "ready local Hermes runtime authority is unavailable"
            )
        if set(authority.required_capabilities).intersection(
            authority.optional_capabilities
        ):
            raise LocalRuntimeAuthorityUnavailable(
                "local Hermes runtime capability authority is invalid"
            )
        return authority

    def _require_runtime_authority(self) -> LocalRuntimeAuthority:
        authority = self._runtime_authority
        if authority is None:
            raise LocalRuntimeAuthorityUnavailable(
                "ready local Hermes runtime authority is unavailable"
            )
        return authority

    def _require_transport_epoch(self) -> str:
        epoch_id = self._transport_epoch_id
        if epoch_id is None:
            raise CloudSessionError("cloud transport epoch is unavailable")
        return epoch_id

    async def _require_current_runtime_authority(self) -> None:
        expected = self._require_runtime_authority()
        try:
            current = await self._read_runtime_authority()
        except LocalRuntimeAuthorityUnavailable:
            await self._disconnect(code=1012, reason="local_runtime_unavailable")
            raise
        if current == expected:
            return
        await self._disconnect(code=1012, reason="local_runtime_changed")
        raise LocalRuntimeAuthorityChanged(
            "local Hermes runtime authority changed; cloud reconnect is required"
        )
