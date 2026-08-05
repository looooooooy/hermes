from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Final
from uuid import UUID

from hermes_connector.adapters.contract_codec import (
    ContractCodecError,
    ContractUnsupported,
    FrameTooLarge,
    InvalidEnvelope,
    InvalidUtf8,
    decode_local_gateway_response,
    encode_local_hello,
)
from hermes_connector.application.capability_negotiation import (
    RequiredCapabilityUnavailable,
)
from hermes_connector.domain.contract_messages import (
    LocalGatewayErrorResponse,
    LocalHello,
    LocalWelcome,
)
from hermes_connector.domain.local_gateway import (
    AgentEndpoint,
    LocalGatewayState,
    LocalRuntimeAuthority,
    transition_local_gateway,
)
from hermes_connector.ports.configuration import LocalGatewayConfigPort
from hermes_connector.ports.local_gateway import (
    AgentDiscoveryPort,
    LocalGatewayConnectionPort,
    LocalGatewayTransportPort,
    LocalSessionStatePort,
)


class LocalDeadlineExceeded(TimeoutError):
    code: Final = 4306
    error_name: Final = "deadline_exceeded_before_effect"

    def __init__(self) -> None:
        super().__init__("local gateway exchange exceeded its deadline")


class LocalGatewayOverloaded(RuntimeError):
    code: Final = 4305
    error_name: Final = "overloaded"

    def __init__(self) -> None:
        super().__init__("local gateway is overloaded")


class LocalRuntimeUnavailable(RuntimeError):
    """No unique trusted Plugin Local Gateway endpoint is available."""

    error_name: Final = "local_runtime_unavailable"
    retryable: Final = True

    def __init__(self) -> None:
        super().__init__(self.error_name)


class _EndpointUnavailable(RuntimeError):
    pass


class LocalGatewayClient:
    """Supervised Local Gateway client with logical/physical separation.

    Logical lifecycle:

        DISCONNECTED -> CONNECTING -> NEGOTIATING -> ACTIVE
             ^                              |
             |                              v
             +-------- RECONCILING <--------+

    Transport v1 is deliberately shorter lived: each discovery poll opens one
    UDS connection, exchanges exactly one Hello/Welcome pair, and closes it.
    EOF after Welcome is normal and is never interpreted as Agent presence.
    """

    name = "local_gateway"

    def __init__(
        self,
        *,
        profile: str,
        client_instance_id: UUID,
        required_capabilities: tuple[str, ...],
        optional_capabilities: tuple[str, ...],
        discovery: AgentDiscoveryPort,
        transport: LocalGatewayTransportPort,
        session_state: LocalSessionStatePort,
        config: LocalGatewayConfigPort,
        expected_endpoint: AgentEndpoint | None = None,
    ) -> None:
        self._profile = profile
        self._client_instance_id = client_instance_id
        self._required_capabilities = required_capabilities
        self._optional_capabilities = optional_capabilities
        self._discovery = discovery
        self._transport = transport
        self._session_state = session_state
        self._config = config
        self._expected_endpoint = expected_endpoint

        self._state = LocalGatewayState.DISCONNECTED
        self._state_history = [self._state]
        self._runtime_generation: str | None = None
        self._accepted_capabilities: tuple[str, ...] = ()
        self._active_endpoint: AgentEndpoint | None = None
        self._unavailable_optional_capabilities: tuple[str, ...] = ()
        self._connection: LocalGatewayConnectionPort | None = None
        self._connection_close_lock = asyncio.Lock()
        self._stop_requested: asyncio.Event | None = None
        self._ready_result: asyncio.Future[bool] | None = None
        self._started = False
        self._run_started = False

    @property
    def state(self) -> LocalGatewayState:
        return self._state

    @property
    def state_history(self) -> tuple[LocalGatewayState, ...]:
        return tuple(self._state_history)

    @property
    def runtime_generation(self) -> str | None:
        return self._runtime_generation

    @property
    def accepted_capabilities(self) -> tuple[str, ...]:
        return self._accepted_capabilities

    @property
    def unavailable_optional_capabilities(self) -> tuple[str, ...]:
        return self._unavailable_optional_capabilities

    async def current_runtime_authority(self) -> LocalRuntimeAuthority | None:
        if not await self.ready():
            return None
        if (
            self._state is not LocalGatewayState.ACTIVE
            or self._runtime_generation is None
            or self._active_endpoint is None
        ):
            return None
        accepted = frozenset(self._accepted_capabilities)
        return LocalRuntimeAuthority(
            profile=self._profile,
            runtime_generation=self._runtime_generation,
            instance_id=self._active_endpoint.instance_id,
            host_bundle_id=self._active_endpoint.host_bundle_id,
            process_identity=self._active_endpoint.process_identity,
            required_capabilities=self._required_capabilities,
            optional_capabilities=tuple(
                capability
                for capability in self._optional_capabilities
                if capability in accepted
            ),
        )

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("local gateway client can only be started once")
        loop = asyncio.get_running_loop()
        self._stop_requested = asyncio.Event()
        self._ready_result = loop.create_future()
        self._started = True

    async def ready(self) -> bool:
        if self._ready_result is None:
            return False
        return await asyncio.shield(self._ready_result)

    async def run(self) -> None:
        if not self._started:
            raise RuntimeError("local gateway client is not started")
        if self._run_started:
            raise RuntimeError("local gateway client run loop already started")
        self._run_started = True

        try:
            await self._establish_initial_session()
            self._resolve_ready(True)
            await self._poll_until_stopped()
        except asyncio.CancelledError:
            self._resolve_ready(False)
            raise
        except BaseException:
            self._resolve_ready(False)
            raise
        finally:
            await self._close_connection()
            self._move_to_disconnected()

    async def drain(self) -> None:
        if self._stop_requested is None:
            return
        if self._state in {
            LocalGatewayState.ACTIVE,
            LocalGatewayState.RECONCILING,
        }:
            self._transition(LocalGatewayState.DRAINING)
        self._stop_requested.set()

    async def stop(self) -> None:
        if self._stop_requested is not None:
            self._stop_requested.set()
        failure: BaseException | None = None
        try:
            for cleanup in (
                self._close_connection,
                self._discovery.aclose,
                self._close_connection,
            ):
                try:
                    await cleanup()
                except BaseException as error:  # noqa: BLE001 - cleanup barrier
                    if failure is None:
                        failure = error
        finally:
            self._move_to_disconnected()
            self._resolve_ready(False)
        if failure is not None:
            raise failure

    async def _establish_initial_session(self) -> None:
        last_error: BaseException | None = None
        for attempt in range(self._config.local_max_reconnect_attempts):
            if self._stop_is_requested():
                return
            try:
                await self._perform_exchange(reconciling=False)
                return
            except (ContractCodecError, RequiredCapabilityUnavailable):
                raise
            except asyncio.CancelledError:
                raise
            except (
                LocalDeadlineExceeded,
                _EndpointUnavailable,
                OSError,
            ) as error:
                last_error = error
                self._move_to_disconnected()
                if attempt + 1 < self._config.local_max_reconnect_attempts:
                    await self._wait_or_stop(self._config.local_reconnect_delay_seconds)
        if isinstance(
            last_error,
            (LocalDeadlineExceeded, LocalGatewayOverloaded),
        ):
            raise last_error
        if isinstance(last_error, (_EndpointUnavailable, OSError)):
            raise LocalRuntimeUnavailable() from last_error
        raise LocalDeadlineExceeded() from last_error

    async def _poll_until_stopped(self) -> None:
        while not self._stop_is_requested():
            await self._wait_or_stop(self._config.local_discovery_poll_interval_seconds)
            if self._stop_is_requested():
                return
            try:
                await self._perform_exchange(
                    reconciling=self._state is LocalGatewayState.ACTIVE
                )
            except asyncio.CancelledError:
                raise
            except (
                ContractCodecError,
                RequiredCapabilityUnavailable,
                LocalGatewayOverloaded,
                LocalDeadlineExceeded,
                _EndpointUnavailable,
                OSError,
            ):
                self._move_to_disconnected()

    async def _perform_exchange(self, *, reconciling: bool) -> None:
        if reconciling:
            self._transition(LocalGatewayState.RECONCILING)
        else:
            self._transition(LocalGatewayState.CONNECTING)

        try:
            async with asyncio.timeout(self._config.local_rpc_deadline_seconds):
                endpoints = await self._discovery.discover(self._profile)
                if len(endpoints) != 1:
                    raise _EndpointUnavailable(
                        "exactly one trusted local gateway endpoint is required"
                    )
                endpoint = endpoints[0]
                if self._expected_endpoint is not None and not _same_endpoint_authority(
                    endpoint,
                    self._expected_endpoint,
                ):
                    raise _EndpointUnavailable("local runtime changed after preflight")

                if not reconciling:
                    # CONNECTING includes discovery and the bounded UDS open.
                    connection = await self._connect(endpoint)
                    self._transition(LocalGatewayState.NEGOTIATING)
                else:
                    connection = await self._connect(endpoint)
                self._connection = connection
                try:
                    response = await connection.exchange(
                        encode_local_hello(self._hello())
                    )
                    decoded = decode_local_gateway_response(response)
                    if isinstance(decoded, LocalGatewayErrorResponse):
                        self._raise_remote_error(decoded)
                    welcome = decoded
                    self._validate_welcome(welcome)
                    if welcome.runtime_generation != endpoint.runtime_generation:
                        raise RequiredCapabilityUnavailable(
                            "local descriptor runtime authority changed"
                        )
                    await self._apply_welcome(welcome)
                    self._active_endpoint = endpoint
                finally:
                    await self._close_connection()
        except TimeoutError:
            raise LocalDeadlineExceeded() from None

        self._transition(LocalGatewayState.ACTIVE)

    async def _connect(
        self,
        endpoint: AgentEndpoint,
    ) -> LocalGatewayConnectionPort:
        try:
            async with asyncio.timeout(self._config.local_connect_timeout_seconds):
                return await self._transport.connect(endpoint)
        except TimeoutError:
            raise LocalDeadlineExceeded() from None
        except (InvalidEnvelope, OSError) as error:
            raise _EndpointUnavailable("trusted local endpoint changed") from error

    def _hello(self) -> LocalHello:
        return LocalHello(
            contract_version=1,
            message_type="local.hello",
            client_instance_id=self._client_instance_id,
            profile=self._profile,
            required_capabilities=self._required_capabilities,
            optional_capabilities=self._optional_capabilities,
        )

    def _validate_welcome(self, welcome: LocalWelcome) -> None:
        if welcome.profile != self._profile:
            raise InvalidEnvelope("local welcome profile does not match request")

        accepted = frozenset(welcome.accepted_capabilities)
        unavailable = frozenset(welcome.unavailable_optional_capabilities)
        required = frozenset(self._required_capabilities)
        optional = frozenset(self._optional_capabilities)
        missing_required = tuple(
            capability
            for capability in self._required_capabilities
            if capability not in accepted
        )
        if missing_required:
            raise RequiredCapabilityUnavailable(missing_required)
        if accepted.intersection(unavailable):
            raise InvalidEnvelope("accepted and unavailable capabilities overlap")
        if not accepted.issubset(required.union(optional)):
            raise InvalidEnvelope("welcome accepts an unrequested capability")
        if unavailable != optional.difference(accepted):
            raise InvalidEnvelope("welcome optional capability partition is incomplete")

    def _raise_remote_error(
        self,
        response: LocalGatewayErrorResponse,
    ) -> None:
        if response.code == 4300:
            raise ContractUnsupported("local gateway rejected contract version")
        if response.code == 4301:
            raise InvalidEnvelope("local gateway rejected envelope")
        if response.code == 4302:
            raise FrameTooLarge()
        if response.code == 4303:
            raise InvalidUtf8()
        if response.code == 4304:
            raise RequiredCapabilityUnavailable(self._required_capabilities)
        if response.code == 4305:
            raise LocalGatewayOverloaded()
        if response.code == 4306:
            raise LocalDeadlineExceeded()
        raise InvalidEnvelope("error code is not valid for local gateway handshake")

    async def _apply_welcome(self, welcome: LocalWelcome) -> None:
        previous_generation = self._runtime_generation
        if (
            previous_generation is not None
            and previous_generation != welcome.runtime_generation
        ):
            await self._session_state.invalidate_runtime(
                previous_generation,
                welcome.runtime_generation,
            )
        self._runtime_generation = welcome.runtime_generation
        self._accepted_capabilities = welcome.accepted_capabilities
        self._unavailable_optional_capabilities = (
            welcome.unavailable_optional_capabilities
        )

    async def _close_connection(self) -> None:
        target = self._connection
        if target is None:
            return
        async with self._connection_close_lock:
            if self._connection is not target:
                return
            await target.close()
            if self._connection is target:
                self._connection = None

    async def _wait_or_stop(self, delay_seconds: float) -> None:
        if self._stop_requested is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._stop_requested.wait(),
                timeout=delay_seconds,
            )

    def _transition(self, target: LocalGatewayState) -> None:
        if self._state is target:
            return
        self._state = transition_local_gateway(self._state, target)
        self._state_history.append(self._state)

    def _move_to_disconnected(self) -> None:
        self._active_endpoint = None
        if self._state is LocalGatewayState.DISCONNECTED:
            return
        self._transition(LocalGatewayState.DISCONNECTED)

    def _resolve_ready(self, result: bool) -> None:
        if self._ready_result is not None and not self._ready_result.done():
            self._ready_result.set_result(result)

    def _stop_is_requested(self) -> bool:
        return self._stop_requested is not None and self._stop_requested.is_set()


def _same_endpoint_authority(
    current: AgentEndpoint,
    expected: AgentEndpoint,
) -> bool:
    return (
        current.pid == expected.pid
        and current.profile == expected.profile
        and current.socket_path == expected.socket_path
        and current.instance_id == expected.instance_id
        and current.runtime_generation == expected.runtime_generation
        and current.host_bundle_id == expected.host_bundle_id
        and current.process_identity == expected.process_identity
        and current.socket_device == expected.socket_device
        and current.socket_inode == expected.socket_inode
    )
