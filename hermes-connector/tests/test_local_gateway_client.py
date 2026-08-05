from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from hermes_connector.adapters.contract_codec import (
    ContractUnsupported,
    FrameTooLarge,
    InvalidEnvelope,
    InvalidUtf8,
    decode_local_hello,
    encode_local_welcome,
)
from hermes_connector.application.capability_negotiation import (
    RequiredCapabilityUnavailable,
)
from hermes_connector.application.local_gateway_client import (
    LocalDeadlineExceeded,
    LocalGatewayClient,
    LocalGatewayOverloaded,
    LocalRuntimeUnavailable,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.contract_messages import LocalWelcome
from hermes_connector.domain.local_gateway import (
    LOCAL_GATEWAY_TRANSITIONS,
    AgentEndpoint,
    LocalGatewayState,
    ProcessIdentityEvidence,
)

CLIENT_ID = UUID("11111111-1111-4111-8111-111111111111")
PLUGIN_INSTANCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROCESS_IDENTITY = ProcessIdentityEvidence(
    start_time_ns=1_000,
    executable_path=Path("/private/runtime/hermes-python"),
    executable_device=41,
    executable_inode=73,
)
ENDPOINT = AgentEndpoint(
    pid=10,
    profile="default",
    socket_path=Path("/private/runtime/gateway.sock"),
    instance_id=PLUGIN_INSTANCE_ID,
    runtime_generation="runtime-1",
    host_bundle_id="com.nousresearch.hermes",
    process_identity=PROCESS_IDENTITY,
    socket_device=51,
    socket_inode=79,
    registry_path=Path("/private/runtime/gateway.json"),
)


def welcome(
    *,
    generation: str = "runtime-1",
    profile: str = "default",
    accepted: tuple[str, ...] = ("session.observe",),
    unavailable: tuple[str, ...] = ("session.control",),
) -> bytes:
    return encode_local_welcome(
        LocalWelcome(
            contract_version=1,
            message_type="local.welcome",
            runtime_generation=generation,
            profile=profile,
            accepted_capabilities=accepted,
            unavailable_optional_capabilities=unavailable,
        )
    )


def error_response(code: int, reason: str, **extra: object) -> bytes:
    value: dict[str, object] = {"error": {"code": code, "reason": reason, **extra}}
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class FakeDiscovery:
    def __init__(
        self,
        endpoints: tuple[AgentEndpoint, ...] = (ENDPOINT,),
        *,
        hangs: bool = False,
    ) -> None:
        self.endpoints = endpoints
        self.hangs = hangs
        self.profiles: list[str] = []
        self.cancelled = 0
        self.close_calls = 0
        self.closed = False

    async def discover(self, profile: str) -> tuple[AgentEndpoint, ...]:
        self.profiles.append(profile)
        if self.hangs:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return self.endpoints

    async def aclose(self) -> None:
        if not self.closed:
            self.close_calls += 1
            self.closed = True


class SequenceDiscovery(FakeDiscovery):
    def __init__(
        self,
        snapshots: tuple[tuple[AgentEndpoint, ...], ...],
    ) -> None:
        super().__init__(())
        self.snapshots = deque(snapshots)

    async def discover(self, profile: str) -> tuple[AgentEndpoint, ...]:
        self.profiles.append(profile)
        if self.snapshots:
            self.endpoints = self.snapshots.popleft()
        return self.endpoints


class FakeConnection:
    def __init__(
        self,
        response: bytes,
        *,
        exchange_hangs: bool = False,
    ) -> None:
        self.response = response
        self.exchange_hangs = exchange_hangs
        self.frames: list[bytes] = []
        self.close_calls = 0
        self.exchange_cancelled = False

    async def exchange(self, frame: bytes) -> bytes:
        self.frames.append(frame)
        if self.exchange_hangs:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.exchange_cancelled = True
                raise
        return self.response

    async def close(self) -> None:
        self.close_calls += 1


class FakeTransport:
    def __init__(self, plans: tuple[FakeConnection | str, ...]) -> None:
        self.plans = deque(plans)
        self.connect_calls = 0
        self.cancelled_connects = 0

    async def connect(self, endpoint: AgentEndpoint) -> FakeConnection:
        self.connect_calls += 1
        plan = self.plans.popleft()
        if plan == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled_connects += 1
                raise
        if not isinstance(plan, FakeConnection):
            raise TypeError(f"unsupported fake plan: {plan}")
        return plan


class RecordingSessionState:
    def __init__(self) -> None:
        self.invalidations: list[tuple[str, str]] = []

    async def invalidate_runtime(
        self,
        previous_generation: str,
        current_generation: str,
    ) -> None:
        self.invalidations.append((previous_generation, current_generation))


def config(**overrides: object) -> ConnectorConfig:
    values: dict[str, object] = {
        "local_connect_timeout_seconds": 0.02,
        "local_rpc_deadline_seconds": 0.02,
        "local_max_reconnect_attempts": 2,
        "local_reconnect_delay_seconds": 0.001,
        "local_discovery_poll_interval_seconds": 0.05,
    }
    values.update(overrides)
    return ConnectorConfig(**values)


def client(
    transport: FakeTransport,
    *,
    discovery: FakeDiscovery | None = None,
    session_state: RecordingSessionState | None = None,
    connector_config: ConnectorConfig | None = None,
    expected_endpoint: AgentEndpoint | None = None,
) -> LocalGatewayClient:
    return LocalGatewayClient(
        profile="default",
        client_instance_id=CLIENT_ID,
        required_capabilities=("session.observe",),
        optional_capabilities=("session.control",),
        discovery=discovery or FakeDiscovery(),
        transport=transport,
        session_state=session_state or RecordingSessionState(),
        config=connector_config or config(),
        expected_endpoint=expected_endpoint,
    )


class LocalGatewayStateTest(unittest.TestCase):
    def test_transition_table_matches_frozen_session_protocol(self) -> None:
        self.assertEqual(
            LOCAL_GATEWAY_TRANSITIONS[LocalGatewayState.DISCONNECTED],
            frozenset({LocalGatewayState.CONNECTING}),
        )
        self.assertEqual(
            LOCAL_GATEWAY_TRANSITIONS[LocalGatewayState.CONNECTING],
            frozenset(
                {
                    LocalGatewayState.NEGOTIATING,
                    LocalGatewayState.DISCONNECTED,
                }
            ),
        )
        self.assertEqual(
            LOCAL_GATEWAY_TRANSITIONS[LocalGatewayState.DRAINING],
            frozenset({LocalGatewayState.DISCONNECTED}),
        )


class LocalGatewayClientTest(unittest.TestCase):
    def test_multiple_generic_endpoints_fail_closed_without_selecting_first(
        self,
    ) -> None:
        async def scenario() -> None:
            second_endpoint = replace(
                ENDPOINT,
                pid=11,
                socket_path=Path("/private/runtime/second.sock"),
                instance_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                registry_path=Path("/private/runtime/second.json"),
            )
            transport = FakeTransport((FakeConnection(welcome()),))
            gateway = client(
                transport,
                discovery=FakeDiscovery((ENDPOINT, second_endpoint)),
                connector_config=config(
                    local_max_reconnect_attempts=1,
                    local_discovery_poll_interval_seconds=60.0,
                ),
            )

            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            ready = await asyncio.wait_for(gateway.ready(), timeout=0.1)
            if ready:
                await gateway.drain()
                await gateway.stop()
                await runner
            else:
                with self.assertRaises(LocalRuntimeUnavailable) as captured:
                    await runner
                self.assertEqual(
                    captured.exception.error_name,
                    "local_runtime_unavailable",
                )
                self.assertTrue(captured.exception.retryable)
                await gateway.stop()

            self.assertFalse(ready)
            self.assertEqual(transport.connect_calls, 0)

        asyncio.run(scenario())

    def test_missing_plugin_descriptor_reports_runtime_unavailable_not_deadline(
        self,
    ) -> None:
        async def scenario() -> None:
            gateway = client(
                FakeTransport(()),
                discovery=FakeDiscovery(()),
                connector_config=config(local_max_reconnect_attempts=1),
            )

            await gateway.start()
            runner = asyncio.create_task(gateway.run())

            self.assertFalse(await asyncio.wait_for(gateway.ready(), timeout=0.1))
            with self.assertRaises(LocalRuntimeUnavailable) as captured:
                await runner
            self.assertEqual(
                captured.exception.error_name,
                "local_runtime_unavailable",
            )
            self.assertTrue(captured.exception.retryable)
            await gateway.stop()

        asyncio.run(scenario())

    def test_connect_time_endpoint_race_reports_runtime_unavailable_not_deadline(
        self,
    ) -> None:
        class FailingTransport:
            def __init__(self, error: BaseException) -> None:
                self.error = error

            async def connect(self, endpoint: AgentEndpoint) -> FakeConnection:
                raise self.error

        async def scenario() -> None:
            for error in (
                InvalidEnvelope("local gateway socket is unavailable"),
                ConnectionRefusedError("local gateway socket disappeared"),
            ):
                with self.subTest(error=type(error).__name__):
                    gateway = client(
                        FailingTransport(error),  # type: ignore[arg-type]
                        connector_config=config(local_max_reconnect_attempts=1),
                    )
                    await gateway.start()
                    runner = asyncio.create_task(gateway.run())

                    self.assertFalse(await gateway.ready())
                    with self.assertRaises(LocalRuntimeUnavailable) as captured:
                        await runner
                    self.assertEqual(
                        captured.exception.error_name,
                        "local_runtime_unavailable",
                    )
                    self.assertTrue(captured.exception.retryable)
                    await gateway.stop()

        asyncio.run(scenario())

    def test_runtime_changed_after_preflight_is_rejected_before_connect(self) -> None:
        async def scenario() -> None:
            changed = replace(
                ENDPOINT,
                instance_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            )
            transport = FakeTransport((FakeConnection(welcome()),))
            gateway = client(
                transport,
                discovery=FakeDiscovery((changed,)),
                expected_endpoint=ENDPOINT,
                connector_config=config(local_max_reconnect_attempts=1),
            )

            await gateway.start()
            with self.assertRaises(LocalRuntimeUnavailable):
                await gateway.run()

            self.assertEqual(transport.connect_calls, 0)
            self.assertFalse(await gateway.ready())
            await gateway.stop()

        asyncio.run(scenario())

    def test_produces_hello_consumes_welcome_and_stops_cleanly(self) -> None:
        async def scenario() -> None:
            connection = FakeConnection(welcome())
            transport = FakeTransport((connection,))
            gateway = client(transport)

            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            self.assertTrue(await gateway.ready())

            hello = decode_local_hello(connection.frames[0])
            self.assertEqual(hello.client_instance_id, CLIENT_ID)
            self.assertEqual(hello.profile, "default")
            self.assertEqual(hello.required_capabilities, ("session.observe",))
            self.assertEqual(hello.optional_capabilities, ("session.control",))
            self.assertEqual(gateway.state, LocalGatewayState.ACTIVE)
            self.assertEqual(gateway.runtime_generation, "runtime-1")
            self.assertEqual(gateway.accepted_capabilities, ("session.observe",))
            self.assertEqual(
                gateway.unavailable_optional_capabilities,
                ("session.control",),
            )
            authority = await gateway.current_runtime_authority()
            self.assertIsNotNone(authority)
            assert authority is not None
            self.assertEqual(authority.runtime_generation, "runtime-1")
            self.assertEqual(
                authority.required_capabilities,
                ("session.observe",),
            )
            self.assertEqual(authority.optional_capabilities, ())

            await gateway.drain()
            await gateway.stop()
            await runner
            self.assertEqual(connection.close_calls, 1)
            self.assertEqual(gateway.state, LocalGatewayState.DISCONNECTED)
            self.assertIsNone(await gateway.current_runtime_authority())

        asyncio.run(scenario())

    def test_repeated_stop_closes_owned_discovery_exactly_once(self) -> None:
        async def scenario() -> None:
            discovery = FakeDiscovery()
            gateway = client(
                FakeTransport((FakeConnection(welcome()),)),
                discovery=discovery,
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            self.assertTrue(await gateway.ready())

            await gateway.stop()
            await gateway.stop()
            await runner

            self.assertTrue(discovery.closed)
            self.assertEqual(discovery.close_calls, 1)
            self.assertIs(gateway.state, LocalGatewayState.DISCONNECTED)

        asyncio.run(scenario())

    def test_connection_close_retries_once_after_failure_and_preserves_error(
        self,
    ) -> None:
        class FailOnceCloseConnection:
            def __init__(self) -> None:
                self.close_calls = 0

            async def exchange(self, frame: bytes) -> bytes:
                raise AssertionError("stop must not exchange")

            async def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("socket close failed first")

        async def scenario() -> None:
            discovery = FakeDiscovery()
            gateway = client(FakeTransport(()), discovery=discovery)
            connection = FailOnceCloseConnection()
            await gateway.start()
            gateway._connection = connection

            with self.assertRaisesRegex(RuntimeError, "socket close failed first"):
                await gateway.stop()

            self.assertTrue(discovery.closed)
            self.assertEqual(discovery.close_calls, 1)
            self.assertEqual(connection.close_calls, 2)
            self.assertIsNone(gateway._connection)
            self.assertIs(gateway.state, LocalGatewayState.DISCONNECTED)

        asyncio.run(scenario())

    def test_two_close_failures_keep_connection_for_next_stop_retry(self) -> None:
        class FailTwiceCloseConnection:
            def __init__(self) -> None:
                self.close_calls = 0

            async def exchange(self, frame: bytes) -> bytes:
                raise AssertionError("stop must not exchange")

            async def close(self) -> None:
                self.close_calls += 1
                if self.close_calls <= 2:
                    raise RuntimeError(f"socket close failed {self.close_calls}")

        async def scenario() -> None:
            discovery = FakeDiscovery()
            gateway = client(FakeTransport(()), discovery=discovery)
            connection = FailTwiceCloseConnection()
            await gateway.start()
            gateway._connection = connection

            with self.assertRaisesRegex(RuntimeError, "socket close failed 1"):
                await gateway.stop()

            self.assertEqual(connection.close_calls, 2)
            self.assertIs(gateway._connection, connection)
            self.assertEqual(discovery.close_calls, 1)

            await gateway.stop()

            self.assertEqual(connection.close_calls, 3)
            self.assertIsNone(gateway._connection)
            self.assertEqual(discovery.close_calls, 1)

        asyncio.run(scenario())

    def test_old_close_completion_does_not_clear_a_new_connection(self) -> None:
        class BlockingCloseConnection:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.close_calls = 0

            async def exchange(self, frame: bytes) -> bytes:
                raise AssertionError("test calls only close")

            async def close(self) -> None:
                self.close_calls += 1
                self.entered.set()
                await self.release.wait()

        class RecordingCloseConnection:
            def __init__(self) -> None:
                self.close_calls = 0

            async def exchange(self, frame: bytes) -> bytes:
                raise AssertionError("test calls only close")

            async def close(self) -> None:
                self.close_calls += 1

        async def scenario() -> None:
            gateway = client(FakeTransport(()))
            previous = BlockingCloseConnection()
            replacement = RecordingCloseConnection()
            gateway._connection = previous

            first_old_close = asyncio.create_task(gateway._close_connection())
            await previous.entered.wait()
            second_old_close = asyncio.create_task(gateway._close_connection())
            await asyncio.sleep(0)
            gateway._connection = replacement
            previous.release.set()
            await asyncio.gather(first_old_close, second_old_close)

            self.assertIs(gateway._connection, replacement)
            self.assertEqual(previous.close_calls, 1)
            self.assertEqual(replacement.close_calls, 0)

            await gateway._close_connection()
            self.assertIsNone(gateway._connection)
            self.assertEqual(replacement.close_calls, 1)

        asyncio.run(scenario())

    def test_concurrent_close_calls_are_single_flight_for_one_connection(
        self,
    ) -> None:
        class BlockingCloseConnection:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.close_calls = 0

            async def exchange(self, frame: bytes) -> bytes:
                raise AssertionError("test calls only close")

            async def close(self) -> None:
                self.close_calls += 1
                self.entered.set()
                await self.release.wait()

        async def scenario() -> None:
            gateway = client(FakeTransport(()))
            connection = BlockingCloseConnection()
            gateway._connection = connection

            first = asyncio.create_task(gateway._close_connection())
            await connection.entered.wait()
            second = asyncio.create_task(gateway._close_connection())
            await asyncio.sleep(0)

            self.assertEqual(connection.close_calls, 1)
            self.assertFalse(first.done())
            self.assertFalse(second.done())

            connection.release.set()
            await asyncio.gather(first, second)

            self.assertEqual(connection.close_calls, 1)
            self.assertIsNone(gateway._connection)

        asyncio.run(scenario())

    def test_run_finally_and_stop_share_one_connection_close_flight(self) -> None:
        class RunFinallyBlockingConnection(FakeConnection):
            def __init__(self) -> None:
                super().__init__(welcome())
                self.close_entered = asyncio.Event()
                self.close_release = asyncio.Event()

            async def close(self) -> None:
                self.close_calls += 1
                self.close_entered.set()
                await self.close_release.wait()

        async def scenario() -> None:
            connection = RunFinallyBlockingConnection()
            discovery = FakeDiscovery()
            gateway = client(
                FakeTransport((connection,)),
                discovery=discovery,
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            await asyncio.wait_for(connection.close_entered.wait(), timeout=0.5)

            stopper = asyncio.create_task(gateway.stop())
            await asyncio.sleep(0)
            self.assertEqual(connection.close_calls, 1)

            connection.close_release.set()
            await asyncio.wait_for(stopper, timeout=0.5)
            await asyncio.wait_for(runner, timeout=0.5)

            self.assertEqual(connection.close_calls, 1)
            self.assertIsNone(gateway._connection)
            self.assertTrue(discovery.closed)
            self.assertIs(gateway.state, LocalGatewayState.DISCONNECTED)

        asyncio.run(scenario())

    def test_required_capability_missing_fails_closed_with_4304(self) -> None:
        async def scenario() -> None:
            connection = FakeConnection(
                welcome(accepted=(), unavailable=("session.control",))
            )
            gateway = client(FakeTransport((connection,)))

            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            self.assertFalse(await gateway.ready())
            with self.assertRaises(RequiredCapabilityUnavailable) as raised:
                await runner
            self.assertEqual(raised.exception.code, 4304)
            self.assertEqual(connection.close_calls, 1)

        asyncio.run(scenario())

    def test_invalid_welcome_fails_with_4301_and_never_becomes_ready(self) -> None:
        async def scenario() -> None:
            invalid_responses = (
                welcome(profile="other"),
                welcome(
                    accepted=("session.observe", "session.control"),
                    unavailable=("session.control",),
                ),
            )
            for response in invalid_responses:
                connection = FakeConnection(response)
                gateway = client(FakeTransport((connection,)))
                await gateway.start()
                runner = asyncio.create_task(gateway.run())

                self.assertFalse(await gateway.ready())
                with self.assertRaises(InvalidEnvelope) as raised:
                    await runner
                self.assertEqual(raised.exception.code, 4301)
                self.assertEqual(connection.close_calls, 1)

        asyncio.run(scenario())

    def test_connect_and_handshake_deadlines_cancel_operations(self) -> None:
        async def scenario() -> None:
            hanging_transport = FakeTransport(("hang", "hang"))
            connect_gateway = client(hanging_transport)
            await connect_gateway.start()
            connect_runner = asyncio.create_task(connect_gateway.run())

            self.assertFalse(await connect_gateway.ready())
            with self.assertRaises(LocalDeadlineExceeded) as connect_error:
                await connect_runner
            self.assertEqual(connect_error.exception.code, 4306)
            self.assertEqual(
                connect_error.exception.error_name,
                "deadline_exceeded_before_effect",
            )
            self.assertEqual(hanging_transport.connect_calls, 2)
            self.assertEqual(hanging_transport.cancelled_connects, 2)

            hanging_connection = FakeConnection(
                welcome(),
                exchange_hangs=True,
            )
            handshake_gateway = client(
                FakeTransport((hanging_connection,)),
                connector_config=config(local_max_reconnect_attempts=1),
            )
            await handshake_gateway.start()
            handshake_runner = asyncio.create_task(handshake_gateway.run())

            self.assertFalse(await handshake_gateway.ready())
            with self.assertRaises(LocalDeadlineExceeded) as rpc_error:
                await handshake_runner
            self.assertEqual(rpc_error.exception.code, 4306)
            self.assertTrue(hanging_connection.exchange_cancelled)
            self.assertEqual(hanging_connection.close_calls, 1)

        asyncio.run(scenario())

    def test_remote_error_response_maps_exact_catalog_codes(self) -> None:
        async def scenario() -> None:
            cases = (
                (4300, "contract_unsupported", ContractUnsupported),
                (4301, "invalid_envelope", InvalidEnvelope),
                (4302, "frame_too_large", FrameTooLarge),
                (4303, "invalid_utf8", InvalidUtf8),
                (
                    4304,
                    "capability_not_available",
                    RequiredCapabilityUnavailable,
                ),
                (4305, "overloaded", LocalGatewayOverloaded),
                (
                    4306,
                    "deadline_exceeded_before_effect",
                    LocalDeadlineExceeded,
                ),
            )
            for code, reason, error_type in cases:
                with self.subTest(code=code):
                    connection = FakeConnection(error_response(code, reason))
                    gateway = client(
                        FakeTransport((connection,)),
                        connector_config=config(local_max_reconnect_attempts=1),
                    )
                    await gateway.start()
                    runner = asyncio.create_task(gateway.run())

                    self.assertFalse(await gateway.ready())
                    with self.assertRaises(error_type) as raised:
                        await runner
                    self.assertEqual(raised.exception.code, code)
                    self.assertEqual(raised.exception.error_name, reason)
                    self.assertEqual(connection.close_calls, 1)

        asyncio.run(scenario())

    def test_malformed_or_operation_only_error_response_fails_with_4301(
        self,
    ) -> None:
        async def scenario() -> None:
            responses = (
                error_response(4304, "overloaded"),
                error_response(
                    4304,
                    "capability_not_available",
                    detail="must-not-leak",
                ),
                error_response(4307, "effect_unknown"),
            )
            for response in responses:
                connection = FakeConnection(response)
                gateway = client(
                    FakeTransport((connection,)),
                    connector_config=config(local_max_reconnect_attempts=1),
                )
                await gateway.start()
                runner = asyncio.create_task(gateway.run())

                self.assertFalse(await gateway.ready())
                with self.assertRaises(InvalidEnvelope) as raised:
                    await runner
                self.assertEqual(raised.exception.code, 4301)
                self.assertEqual(connection.close_calls, 1)

        asyncio.run(scenario())

    def test_discovery_deadline_cancels_operation_with_4306(self) -> None:
        async def scenario() -> None:
            discovery = FakeDiscovery(hangs=True)
            gateway = client(
                FakeTransport(()),
                discovery=discovery,
                connector_config=config(local_max_reconnect_attempts=1),
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())

            self.assertFalse(await gateway.ready())
            with self.assertRaises(LocalDeadlineExceeded) as raised:
                await runner
            self.assertEqual(raised.exception.code, 4306)
            self.assertEqual(discovery.cancelled, 1)

        asyncio.run(scenario())

    def test_cancellation_propagates_and_leaves_no_connection(self) -> None:
        async def scenario() -> None:
            transport = FakeTransport(("hang",))
            gateway = client(
                transport,
                connector_config=config(local_max_reconnect_attempts=1),
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            await asyncio.sleep(0)
            runner.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await runner
            self.assertEqual(transport.cancelled_connects, 1)
            self.assertEqual(gateway.state, LocalGatewayState.DISCONNECTED)
            await gateway.stop()

        asyncio.run(scenario())

    def test_reconnect_generation_change_invalidates_stale_session_state(
        self,
    ) -> None:
        async def scenario() -> None:
            first = FakeConnection(welcome(generation="runtime-1"))
            second = FakeConnection(welcome(generation="runtime-2"))
            session_state = RecordingSessionState()
            discovery = FakeDiscovery()
            gateway = client(
                FakeTransport((first, second)),
                discovery=discovery,
                session_state=session_state,
                connector_config=config(local_discovery_poll_interval_seconds=0.02),
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            self.assertTrue(await gateway.ready())
            discovery.endpoints = (replace(ENDPOINT, runtime_generation="runtime-2"),)

            for _ in range(100):
                if gateway.runtime_generation == "runtime-2":
                    break
                await asyncio.sleep(0.001)

            self.assertEqual(gateway.runtime_generation, "runtime-2")
            self.assertEqual(
                session_state.invalidations,
                [("runtime-1", "runtime-2")],
            )
            self.assertIn(LocalGatewayState.RECONCILING, gateway.state_history)

            await gateway.drain()
            await gateway.stop()
            await runner
            self.assertEqual(first.close_calls, 1)
            self.assertEqual(second.close_calls, 1)

        asyncio.run(scenario())

    def test_successful_handshake_eof_is_not_a_presence_failure(self) -> None:
        async def scenario() -> None:
            connection = FakeConnection(welcome())
            gateway = client(
                FakeTransport((connection,)),
                connector_config=config(local_discovery_poll_interval_seconds=60.0),
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())

            self.assertTrue(await gateway.ready())
            self.assertEqual(gateway.state, LocalGatewayState.ACTIVE)
            self.assertFalse(runner.done())
            self.assertEqual(connection.close_calls, 1)

            await gateway.drain()
            await gateway.stop()
            await runner

        asyncio.run(scenario())

    def test_descriptor_disappearance_retries_and_recovers_via_polling(
        self,
    ) -> None:
        async def scenario() -> None:
            discovery = SequenceDiscovery(((ENDPOINT,), (), (ENDPOINT,)))
            first = FakeConnection(welcome())
            recovered = FakeConnection(welcome())
            gateway = client(
                FakeTransport((first, recovered)),
                discovery=discovery,
                connector_config=config(local_discovery_poll_interval_seconds=0.02),
            )
            await gateway.start()
            runner = asyncio.create_task(gateway.run())
            self.assertTrue(await gateway.ready())

            for _ in range(100):
                if recovered.close_calls == 1:
                    break
                await asyncio.sleep(0.001)

            self.assertEqual(recovered.close_calls, 1)
            self.assertGreater(
                gateway.state_history.count(LocalGatewayState.DISCONNECTED),
                1,
            )
            self.assertEqual(gateway.state, LocalGatewayState.ACTIVE)

            await gateway.drain()
            await gateway.stop()
            await runner

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
