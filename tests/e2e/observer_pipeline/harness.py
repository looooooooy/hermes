"""Contract-faithful gateway-extension/1 Host test double and real I/O harness.

This module is deliberately not a Hermes Core substitute claim. Hermes 0.19 does
not publish gateway-extension/1, so the Host itself is the only test double. The
Plugin relay, Connector clients/codecs/state machines/storage, Cloud ASGI apps,
and Cloud ORM repositories used by the E2E remain production implementations.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    LocalContractV1Adapter,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)
from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    capture_macos_host_authority,
)
from hermes_agent_plugin.bootstrap.registration import register
from hermes_agent_plugin.ports import local_relay as plugin_local_relay

PROFILE = "default"
RUNTIME_GENERATION = "71000000-0000-4000-8000-000000000001"
SESSION_A = "android-bootstrap"
SESSION_B = "observer-second-session"
RUNTIME_SESSION_A = "81000000-0000-4000-8000-000000000001"
RUNTIME_SESSION_B = "82000000-0000-4000-8000-000000000001"
OUTPUT_PARITY_CAPABILITY = "session.observe.output-parity.v1"
LIVE_HOST_ENV = "HERMES_E2E_LIVE_HOST"


def require_live_host_spi() -> None:
    """Skip unless the opt-in live Host SPI environment is available.

    The gateway-extension Host doubles still register the production plugin,
    whose public DTO contract lives in ``hermes_cli.extension_host_v1``; without
    a real Hermes source tree on the import path those pipelines fail closed by
    design, so the tests gate on an explicit opt-in plus importability.
    """
    if os.environ.get(LIVE_HOST_ENV) is None:
        pytest.skip(f"set {LIVE_HOST_ENV}=1 to run the live Host SPI pipeline")
    if importlib.util.find_spec("hermes_cli") is None or (
        importlib.util.find_spec("hermes_cli.extension_host_v1") is None
    ):
        pytest.skip("hermes_cli.extension_host_v1 is not importable")


class CloseRegistration:
    def __init__(self, close: Callable[[], None] = lambda: None) -> None:
        self._close = close
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close()


class ObserverTransportSink:
    """Host transport sink matching the gateway-extension/1 observer surface."""

    def __init__(self, transport: object) -> None:
        self._transport = transport

    def __call__(self, event: Mapping[str, object]) -> bool:
        return self.on_event(event)

    def on_event(self, event: Mapping[str, object]) -> bool:
        return bool(
            self._transport.write(
                {"jsonrpc": "2.0", "method": "event", "params": dict(event)}
            )
        )

    def on_snapshot(self, snapshot: Mapping[str, object]) -> bool:
        return bool(
            self._transport.write(
                {"jsonrpc": "2.0", "method": "snapshot", "params": dict(snapshot)}
            )
        )


class AuthoritativeSession:
    def __init__(self, session_key: str, runtime_session_id: str) -> None:
        self.session_key = session_key
        self.runtime_session_id = runtime_session_id
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._event_sequence = 0
        self._messages: list[dict[str, object]] = [
            {"role": "assistant", "content": f"权威快照：{session_key}"}
        ]
        self._sinks: set[Callable[[dict[str, object]], object]] = set()
        self.prepare_count = 0
        self.close_count = 0

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sinks)

    @property
    def event_sequence(self) -> int:
        with self._lock:
            return self._event_sequence

    def prepare(self, sink: Callable[[dict[str, object]], object]) -> PreparedSession:
        with self._lock:
            self.prepare_count += 1
            snapshot = self._snapshot_locked()
            self._condition.notify_all()
        return PreparedSession(self, sink, snapshot)

    def wait_for_prepare_count(self, expected: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self.prepare_count < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wait_for_active_count(self, expected: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._sinks) != expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def emit(
        self,
        *,
        event_type: str,
        payload: Mapping[str, object],
        event_sequence: int | None = None,
        event_sequence_start: int | None = None,
    ) -> int:
        with self._lock:
            sequence = (
                self._event_sequence + 1 if event_sequence is None else event_sequence
            )
            self._event_sequence = sequence
            if event_type in {"message.delta", "message.complete"}:
                text = payload.get("text")
                if isinstance(text, str):
                    self._messages.append({"role": "assistant", "content": text})
            sinks = tuple(self._sinks)
            event = {
                "profile": PROFILE,
                "runtime_generation": RUNTIME_GENERATION,
                "session_key": self.session_key,
                "session_id": self.runtime_session_id,
                "type": event_type,
                "event_sequence": sequence,
                "payload": dict(payload),
            }
            if event_sequence_start is not None:
                event["event_sequence_start"] = event_sequence_start
        for sink in sinks:
            sink(copy.deepcopy(event))
        return sequence

    def _snapshot_locked(self) -> dict[str, object]:
        return {
            "profile": PROFILE,
            "runtime_generation": RUNTIME_GENERATION,
            "session_key": self.session_key,
            "runtime_session_id": self.runtime_session_id,
            "running": True,
            "status": "running",
            "event_sequence": self._event_sequence,
            "snapshot_event_sequence": self._event_sequence,
            "messages": copy.deepcopy(self._messages),
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "replay_events": [],
        }

    def _activate(
        self,
        sink: Callable[[dict[str, object]], object],
    ) -> CloseRegistration:
        with self._lock:
            self._sinks.add(sink)
            self._condition.notify_all()

        def close() -> None:
            with self._lock:
                if sink in self._sinks:
                    self._sinks.remove(sink)
                    self.close_count += 1
                    self._condition.notify_all()

        return CloseRegistration(close)


class AuthoritativeV2Session(AuthoritativeSession):
    """Authoritative Host-double session emitting exact Observer v2 facts."""

    def __init__(self, session_key: str, runtime_session_id: str) -> None:
        super().__init__(session_key, runtime_session_id)
        self._event_sequence = 5
        self._current_todo_sections = [
            {
                "turn_id": "turn-1",
                "section_id": "todo-1",
                "revision": 2,
                "first_event_sequence": 1,
                "status": "completed",
                "items": [
                    {
                        "id": "todo-item-1",
                        "label": "Run production E2E",
                        "status": "completed",
                    }
                ],
            }
        ]
        self._current_subagents = [
            {
                "turn_id": "turn-1",
                "subagent_id": "subagent-1",
                "revision": 1,
                "first_event_sequence": 2,
                "parent_subagent_id": None,
                "name": "Pipeline verifier",
                "goal": "Verify the production observer path",
                "summary": None,
                "status": "running",
            }
        ]
        self._current_tools = [
            {
                "turn_id": "turn-1",
                "tool_call_id": "tool-1",
                "revision": 1,
                "first_event_sequence": 3,
                "status": "running",
                "name": "E2E tests",
            }
        ]
        self._current_terminals = [
            {
                "turn_id": "turn-1",
                "process_id": "process-1",
                "revision": 1,
                "first_event_sequence": 4,
                "status": "running",
            }
        ]

    def _snapshot_locked(self) -> dict[str, object]:
        if self.prepare_count > 1:
            return {
                "observer_contract": 2,
                "profile": PROFILE,
                "runtime_generation": RUNTIME_GENERATION,
                "session_key": self.session_key,
                "runtime_session_id": self.runtime_session_id,
                "running": True,
                "status": "running",
                "event_sequence": self._event_sequence,
                "snapshot_event_sequence": self._event_sequence,
                "messages": copy.deepcopy(self._messages),
                "inflight": {
                    "user": None,
                    "assistant": None,
                    "streaming": False,
                    "error": None,
                },
                "todo_sections": copy.deepcopy(self._current_todo_sections),
                "subagents": copy.deepcopy(self._current_subagents),
                "tools": copy.deepcopy(self._current_tools),
                "terminals": copy.deepcopy(self._current_terminals),
                "replay_events": [],
            }
        return {
            "observer_contract": 2,
            "profile": PROFILE,
            "runtime_generation": RUNTIME_GENERATION,
            "session_key": self.session_key,
            "runtime_session_id": self.runtime_session_id,
            "running": True,
            "status": "running",
            "event_sequence": 5,
            "snapshot_event_sequence": 4,
            "messages": copy.deepcopy(self._messages),
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "todo_sections": [
                {
                    "turn_id": "turn-1",
                    "section_id": "todo-1",
                    "revision": 1,
                    "first_event_sequence": 1,
                    "status": "in_progress",
                    "items": [
                        {
                            "id": "todo-item-1",
                            "label": "Run production E2E",
                            "status": "in_progress",
                        }
                    ],
                }
            ],
            "subagents": [
                {
                    "turn_id": "turn-1",
                    "subagent_id": "subagent-1",
                    "revision": 1,
                    "first_event_sequence": 2,
                    "parent_subagent_id": None,
                    "name": "Pipeline verifier",
                    "goal": "Verify the production observer path",
                    "summary": None,
                    "status": "running",
                }
            ],
            "tools": [
                {
                    "turn_id": "turn-1",
                    "tool_call_id": "tool-1",
                    "revision": 1,
                    "first_event_sequence": 3,
                    "status": "running",
                    "name": "E2E tests",
                }
            ],
            "terminals": [
                {
                    "turn_id": "turn-1",
                    "process_id": "process-1",
                    "revision": 1,
                    "first_event_sequence": 4,
                    "status": "running",
                }
            ],
            "replay_events": [
                {
                    "observer_contract": 2,
                    "profile": PROFILE,
                    "runtime_generation": RUNTIME_GENERATION,
                    "session_key": self.session_key,
                    "session_id": self.runtime_session_id,
                    "type": "todo.update",
                    "event_sequence": 5,
                    "payload": {
                        "turn_id": "turn-1",
                        "section_id": "todo-1",
                        "revision": 2,
                        "first_event_sequence": 1,
                        "operation": "upsert",
                        "status": "completed",
                        "items": [
                            {
                                "id": "todo-item-1",
                                "label": "Run production E2E",
                                "status": "completed",
                            }
                        ],
                    },
                }
            ],
        }

    def emit(
        self,
        *,
        event_type: str,
        payload: Mapping[str, object],
        event_sequence: int | None = None,
        event_sequence_start: int | None = None,
    ) -> int:
        sequence = super().emit(
            event_type=event_type,
            payload=payload,
            event_sequence=event_sequence,
            event_sequence_start=event_sequence_start,
        )
        collection_and_identity = {
            "todo.update": (self._current_todo_sections, "section_id"),
            "subagent.update": (self._current_subagents, "subagent_id"),
            "tool.update": (self._current_tools, "tool_call_id"),
            "terminal.update": (self._current_terminals, "process_id"),
        }.get(event_type)
        if collection_and_identity is None or payload.get("operation") != "upsert":
            return sequence
        collection, identity_field = collection_and_identity
        candidate = {
            key: copy.deepcopy(value)
            for key, value in payload.items()
            if key != "operation"
        }
        with self._lock:
            collection[:] = [
                item
                for item in collection
                if item.get(identity_field) != candidate.get(identity_field)
                or item.get("turn_id") != candidate.get("turn_id")
            ]
            collection.append(candidate)
        return sequence

    def inject_sensitive_extension(
        self,
        *credential_like_values: str,
    ) -> None:
        """Offer an invalid private extension without mutating Host authority."""

        with self._lock:
            event = {
                "observer_contract": 2,
                "profile": PROFILE,
                "runtime_generation": RUNTIME_GENERATION,
                "session_key": self.session_key,
                "session_id": self.runtime_session_id,
                "type": "status.update",
                "event_sequence": self._event_sequence + 1,
                "payload": {"status": "running", "running": True},
                "extensions": {
                    "vendor.display": {"notes": list(credential_like_values)}
                },
            }
            sinks = tuple(self._sinks)
        for sink in sinks:
            sink(copy.deepcopy(event))


class PreparedSession:
    def __init__(
        self,
        session: AuthoritativeSession,
        sink: Callable[[dict[str, object]], object],
        snapshot: dict[str, object],
    ) -> None:
        self.snapshot = MappingProxyType(snapshot)
        self.activation_deadline_monotonic = time.monotonic() + 2.0
        self._session = session
        self._sink = sink
        self._lock = threading.Lock()
        self._state = "prepared"
        self._registration: CloseRegistration | None = None
        self._timer = threading.Timer(2.0, self.close)
        self._timer.daemon = True
        self._timer.start()

    def activate(self) -> CloseRegistration:
        with self._lock:
            if self._state != "prepared" or time.monotonic() >= (
                self.activation_deadline_monotonic
            ):
                self._state = "closed"
                self._timer.cancel()
                raise RuntimeError("prepared Observer activation expired")
            self._state = "active"
            self._timer.cancel()
            self._registration = self._session._activate(self._sink)
            return self._registration

    def close(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            registration = self._registration
            self._state = "closed"
            self._timer.cancel()
        if registration is not None:
            registration.close()


class GatewayExtensionV1TestHost:
    """Exact public Host SPI v1 test double; not a real Hermes connection."""

    host_api_version = 1

    def __init__(self, paths: MacOSLocalGatewayPaths) -> None:
        self._paths = paths
        self._backend = MacOSLocalRelayBackend(paths)
        self._authority = capture_macos_host_authority(
            profile=PROFILE,
            host_bundle_id="com.nousresearch.hermes",
        ).bind_runtime(RUNTIME_GENERATION)
        self._lock = threading.RLock()
        self._listeners: list[object] = []
        self._transport_subscriptions: dict[object, dict[str, object]] = {}
        self._resources: list[CloseRegistration] = []
        self._runtime_resource: object | None = None
        self._extension_registration: object | None = None
        self._previous_local_relay_backend_factory: Callable[[], object] | None = None
        self._owns_local_relay_backend_factory = False
        self.audits: list[str] = []
        self.sessions = {
            SESSION_A: AuthoritativeSession(SESSION_A, RUNTIME_SESSION_A),
            SESSION_B: AuthoritativeSession(SESSION_B, RUNTIME_SESSION_B),
        }

    def start(self) -> None:
        adapter = LocalContractV1Adapter(
            runtime_generation=RUNTIME_GENERATION,
            available_capabilities=self.local_gateway_capabilities(),
        )
        resource = create_local_gateway_resource(
            paths=self._paths,
            authority=self._authority,
            hello_handler=adapter.handle_hello,
            ready=lambda: True,
            clock=time.monotonic,
        )
        resource.start(time.monotonic() + 2.0)
        self._runtime_resource = resource
        context = GatewayExtensionV1TestContext(self)
        self._previous_local_relay_backend_factory = plugin_local_relay._backend_factory
        self._owns_local_relay_backend_factory = True
        try:
            register(context)
        except BaseException:
            self._restore_local_relay_backend_factory()
            raise
        self._extension_registration = context.registration

    def local_gateway_capabilities(self) -> frozenset[str]:
        return frozenset({"session.observe", "session.control"})

    def _restore_local_relay_backend_factory(self) -> None:
        if not self._owns_local_relay_backend_factory:
            return
        plugin_local_relay._backend_factory = self._previous_local_relay_backend_factory
        self._previous_local_relay_backend_factory = None
        self._owns_local_relay_backend_factory = False

    def close(self) -> None:
        failure: BaseException | None = None
        try:
            registration = self._extension_registration
            self._extension_registration = None
            if registration is not None:
                try:
                    registration.close()
                except Exception as error:  # noqa: BLE001 - continue bounded cleanup
                    failure = error
            resource = self._runtime_resource
            self._runtime_resource = None
            if resource is not None:
                try:
                    resource.stop(time.monotonic() + 2.0)
                except Exception as error:  # noqa: BLE001 - preserve first cleanup failure
                    failure = failure or error
        finally:
            self._restore_local_relay_backend_factory()
        if failure is not None:
            raise failure

    def runtime_descriptor(self) -> object:
        return SimpleNamespace(
            profile=PROFILE,
            runtime_generation=RUNTIME_GENERATION,
            state="ready",
            capabilities=frozenset(
                {
                    "approval.respond",
                    "clarify.respond",
                    "prompt.submit",
                    "session.control",
                    "session.interrupt",
                    "session.observe",
                    "session.steer",
                }
            ),
        )

    def register_local_endpoint(self, endpoint: object) -> CloseRegistration:
        role = endpoint.connection_role
        if role == "control":
            registration = CloseRegistration()
        elif role == "observer":
            raw = self._backend.start_observer_endpoint(
                authority=self._authority,
                dispatch=lambda request, transport: self._dispatch_observer(
                    endpoint, request, transport
                ),
                remove_observer_subscriptions=self._close_transport,
            )
            registration = CloseRegistration(raw.close)
        else:
            raise ValueError("unknown Host local endpoint role")
        self._resources.append(registration)
        return registration

    def prepare_observer(self, request: object, sink: object) -> PreparedSession:
        session_key = request.durable_session_key
        if (
            request.profile != PROFILE
            or request.runtime_generation != RUNTIME_GENERATION
        ):
            raise ValueError("Observer Host binding mismatch")
        session = self.sessions.get(session_key)
        if session is None or not callable(sink):
            raise ValueError("authoritative session is unavailable")
        return session.prepare(sink)

    def control_snapshot(self, _scope: object) -> object:
        return SimpleNamespace(control_revision=0)

    def invoke_owner_action(self, _request: object) -> object:
        raise RuntimeError("owner actions are outside Observer E2E")

    def add_runtime_listener(self, listener: object) -> CloseRegistration:
        self._listeners.append(listener)
        return CloseRegistration(lambda: self._listeners.remove(listener))

    def audit(self, event: object) -> None:
        self.audits.append(str(event.name))

    def _dispatch_observer(
        self,
        endpoint: object,
        request: dict[str, Any],
        transport: object,
    ) -> dict[str, object]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params")
        if method == "session.observe.unsubscribe":
            subscription_id = (
                params.get("subscription_id") if isinstance(params, dict) else None
            )
            with self._lock:
                registration = self._transport_subscriptions.get(transport, {}).pop(
                    str(subscription_id), None
                )
            if registration is not None:
                registration.close()
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": (
                    {"observer_contract": 2}
                    if isinstance(params, dict) and params.get("observer_contract") == 2
                    else {}
                ),
            }
        if method != "session.observe.subscribe" or not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "invalid params"},
            }

        sink = (
            ObserverTransportSink(transport)
            if params.get("observer_contract") == 2
            else lambda event: transport.write(
                {"jsonrpc": "2.0", "method": "event", "params": event}
            )
        )
        prepared = endpoint.prepare_observer(params, sink)
        subscription_id = str(uuid4())
        result = dict(prepared.snapshot)
        result["subscription_id"] = subscription_id
        response: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        original_write = transport.write
        activated = False

        def write_then_activate(frame: dict[str, object]) -> bool:
            nonlocal activated
            written = bool(original_write(frame))
            if frame is response and not activated:
                activated = True
                if not written:
                    prepared.close()
                else:
                    registration = prepared.activate()
                    with self._lock:
                        self._transport_subscriptions.setdefault(transport, {})[
                            subscription_id
                        ] = registration
            return written

        transport.write = write_then_activate
        return response

    def _close_transport(self, transport: object) -> None:
        with self._lock:
            registrations = tuple(
                self._transport_subscriptions.pop(transport, {}).values()
            )
        for registration in registrations:
            registration.close()


class GatewayExtensionV2TestHost(GatewayExtensionV1TestHost):
    """Exact gateway-extension/1 Host double with output-parity enabled."""

    def __init__(self, paths: MacOSLocalGatewayPaths) -> None:
        super().__init__(paths)
        self.sessions = {
            SESSION_A: AuthoritativeV2Session(SESSION_A, RUNTIME_SESSION_A),
            SESSION_B: AuthoritativeV2Session(SESSION_B, RUNTIME_SESSION_B),
        }

    def runtime_descriptor(self) -> object:
        descriptor = super().runtime_descriptor()
        return SimpleNamespace(
            profile=descriptor.profile,
            runtime_generation=descriptor.runtime_generation,
            state=descriptor.state,
            capabilities=frozenset(
                {*descriptor.capabilities, OUTPUT_PARITY_CAPABILITY}
            ),
        )

    def local_gateway_capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                "session.observe",
                "session.control",
                OUTPUT_PARITY_CAPABILITY,
            }
        )

    def prepare_observer(self, request: object, sink: object) -> PreparedSession:
        if getattr(request, "observer_contract", None) != 2 or getattr(
            request, "required_capabilities", frozenset()
        ) != frozenset({OUTPUT_PARITY_CAPABILITY}):
            raise ValueError("Observer v2 Host request is not exact")
        return super().prepare_observer(request, sink)


class GatewayExtensionV1TestContext:
    gateway_extension_spi_version = 1
    gateway_extension_capabilities = frozenset(
        {
            "audit.safe.v1",
            "extension.lifecycle.v1",
            "runtime.descriptor.v1",
            "session.observe.v1",
            "session.owner-actions.v1",
        }
    )

    def __init__(self, host: GatewayExtensionV1TestHost) -> None:
        self._host = host
        self.registration: object | None = None

    def register_gateway_extension(
        self,
        extension: object,
        *,
        spi_version: int,
    ) -> None:
        if spi_version != 1:
            raise RuntimeError("unexpected Host SPI version")
        self.registration = extension.install(self._host)


@dataclass
class RunningAsgiServer:
    application: object
    host: str = "127.0.0.1"

    async def start(self) -> int:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, 0))
        self._listener.listen()
        self.port = int(self._listener.getsockname()[1])
        self._started = asyncio.Event()
        wrapped = _LifespanStarted(self.application, self._started)
        self._server = uvicorn.Server(
            uvicorn.Config(
                wrapped,
                host=self.host,
                port=self.port,
                lifespan="on",
                log_config=None,
                access_log=False,
            )
        )
        self._task = asyncio.create_task(
            self._server.serve(sockets=[self._listener]),
            name=f"e2e-uvicorn:{self.port}",
        )
        await asyncio.wait_for(self._started.wait(), timeout=5)
        return self.port

    async def close(self) -> None:
        self._server.should_exit = True
        await asyncio.wait_for(self._task, timeout=5)


class _LifespanStarted:
    def __init__(self, application: object, started: asyncio.Event) -> None:
        self._application = application
        self._started = started

    async def __call__(self, scope: dict[str, object], receive: object, send: object):
        async def signaling_send(message: dict[str, object]) -> None:
            await send(message)
            if message.get("type") == "lifespan.startup.complete":
                self._started.set()

        await self._application(
            scope,
            receive,
            signaling_send if scope.get("type") == "lifespan" else send,
        )


def local_paths(root: Path) -> MacOSLocalGatewayPaths:
    short = Path("/tmp").resolve(strict=True) / (
        f"hmo-e2e-{os.getpid()}-{root.name[-6:]}"
    )
    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=short / "lr",
        local_gateway_socket_directory=short / "ls",
        control_registry_directory=short / "cr",
        control_socket_directory=short / "cs",
        observer_registry_directory=short / "or",
        observer_socket_directory=short / "os",
    )


def assert_canonical_uuid(value: object) -> UUID:
    assert isinstance(value, str), "identity is not text"
    parsed = UUID(value)
    if str(parsed) != value:
        raise AssertionError("identity is not canonical RFC 4122 UUID")
    return parsed


def private_file(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def keyring_file(path: Path, tenant_id: UUID) -> Path:
    return private_file(
        path,
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    str(tenant_id): {
                        "current": "e2e-v1",
                        "keys": {
                            "e2e-v1": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="
                        },
                    }
                },
            },
            separators=(",", ":"),
        ),
    )


def live_noncurrent_tasks() -> tuple[asyncio.Task[object], ...]:
    current = asyncio.current_task()
    return tuple(
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    )


def live_thread_names() -> frozenset[str]:
    return frozenset(
        thread.name for thread in threading.enumerate() if thread.is_alive()
    )


def open_fd_count() -> int:
    return len(os.listdir("/dev/fd"))


__all__ = [
    "LIVE_HOST_ENV",
    "OUTPUT_PARITY_CAPABILITY",
    "PROFILE",
    "RUNTIME_GENERATION",
    "RUNTIME_SESSION_A",
    "RUNTIME_SESSION_B",
    "SESSION_A",
    "SESSION_B",
    "GatewayExtensionV1TestHost",
    "GatewayExtensionV2TestHost",
    "RunningAsgiServer",
    "assert_canonical_uuid",
    "keyring_file",
    "live_noncurrent_tasks",
    "live_thread_names",
    "local_paths",
    "open_fd_count",
    "private_file",
    "require_live_host_spi",
]
