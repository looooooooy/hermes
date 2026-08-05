"""Strict role-aware WebSocket compatibility adapter."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from hermes_cloud.contracts.mobile_control import CONTROL_ERROR_CODES
from hermes_cloud.contracts.observer_v2 import (
    ObserverV2ContractError,
    require_cloud_frame,
)
from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
)
from hermes_cloud.modules.cloud_api.application.sessions import (
    SessionNotFound,
    SessionQueryService,
)
from hermes_cloud.modules.cloud_api.domain import (
    ObserverSubscription,
    ObserverSubscriptionCapacityExceeded,
    Principal,
    WebSocketTicketAuthentication,
    is_canonical_rfc4122_uuid_v1_to_v5,
)
from hermes_cloud.modules.cloud_api.ports import (
    ObserverSubscriptionPort,
    ProjectionEventSourcePort,
)
from hermes_cloud.modules.control.domain import (
    ControlRequestContext,
    ControlRpcError,
)
from hermes_cloud.modules.control.ports import ControlRuntimePort

_SUBPROTOCOL = "hermes.tui.v1"
_OBSERVER_V2_SUBPROTOCOL = "hermes.tui.v2"
_MAX_FRAME_BYTES = 262_144
_MAX_STRING_BYTES = 131_072
_MAX_DEPTH = 32
_MAX_FIELDS = 1024
_MAX_ITEMS = 1024
_SEND_TIMEOUT_SECONDS = 5.0
_FORWARD_CANCEL_TIMEOUT_SECONDS = 0.25
_INITIAL_SNAPSHOT_TIMEOUT_SECONDS = 10.0
_INITIAL_SNAPSHOT_POLL_SECONDS = 0.05
_LEASE_RENEW_INTERVAL_SECONDS = 30.0
_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "agent.terminal.output",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}
_MERGEABLE_EVENT_TYPES = {
    "agent.terminal.output",
    "message.delta",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}
_RUNNING_STATUSES = {"running", "working", "streaming"}
_CONTROL_ERROR_CODES = dict(CONTROL_ERROR_CODES)


class _PeerSendFailed(RuntimeError):
    """The peer stopped accepting bounded outbound WebSocket frames."""


class _EmptyProjectionEventSource:
    async def events(
        self,
        *,
        tenant_id: object,
        user_id: object,
        session_key: str,
        profile: str | None,
        after_sequence: int,
        agent_id: UUID | None = None,
    ):
        del tenant_id, user_id, session_key, profile, after_sequence, agent_id
        if False:
            yield {}


def register_realtime_route(
    application: FastAPI,
    *,
    authentication: CloudApiService,
    sessions: SessionQueryService | None,
    event_source: ProjectionEventSourcePort | None,
    subscription_manager: ObserverSubscriptionPort | None = None,
    control_runtime: ControlRuntimePort | None = None,
) -> None:
    source = event_source or _EmptyProjectionEventSource()

    async def observer_socket(websocket: WebSocket) -> None:
        raw_ticket = websocket.query_params.get("ticket")
        if (
            raw_ticket is None
            or len(raw_ticket.encode()) > 4096
            or set(websocket.query_params) != {"ticket"}
        ):
            await websocket.close(code=1008)
            return
        try:
            authenticated = await run_in_threadpool(
                authentication.consume_websocket_ticket,
                raw_ticket,
            )
        except AuthenticationFailed:
            await websocket.close(code=1008)
            return

        if authenticated.connection_role == "control":
            if (
                sessions is None
                or authenticated.session_id is None
                or authenticated.session_key is not None
                or authenticated.profile is None
                or authenticated.agent_id is None
            ):
                await websocket.close(code=1008)
                return
            try:
                binding = await run_in_threadpool(
                    sessions.catalog_session_binding,
                    principal=authenticated.principal,
                    session_id=authenticated.session_id,
                    agent_id=authenticated.agent_id,
                    profile=authenticated.profile,
                )
            except SessionNotFound:
                await websocket.close(code=1008)
                return
            if (
                binding.session_id != authenticated.session_id
                or binding.agent_id != authenticated.agent_id
                or binding.profile != authenticated.profile
            ):
                await websocket.close(code=1008)
                return
            authenticated = replace(
                authenticated,
                session_key=binding.session_key,
                profile=binding.profile,
                agent_id=binding.agent_id,
            )

        expected_subprotocol = (
            _OBSERVER_V2_SUBPROTOCOL
            if authenticated.connection_role == "observer"
            and authenticated.observer_contract == 2
            else _SUBPROTOCOL
        )
        if tuple(websocket.scope.get("subprotocols", ())) != (expected_subprotocol,):
            await websocket.close(code=1002)
            return
        await websocket.accept(subprotocol=expected_subprotocol)
        if authenticated.connection_role == "control":
            await _serve_control_socket(
                websocket,
                authenticated,
                control_runtime,
            )
            return

        principal = authenticated.principal
        observer_contract = authenticated.observer_contract
        send_lock = asyncio.Lock()
        event_task: asyncio.Task[None] | None = None
        active_subscription: str | None = None
        active_handle: ObserverSubscription | None = None
        try:
            ready = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "observer_contract": observer_contract,
                        "connection_role": "observer",
                    },
                },
            }
            if observer_contract == 2:
                require_cloud_frame("gateway_ready", ready)
            await _send_json(websocket, ready, send_lock)
            while True:
                message = await _receive_with_forward_supervision(
                    websocket,
                    event_task,
                )
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await websocket.close(code=1002)
                    break
                raw_frame = message.get("text")
                if not isinstance(raw_frame, str):
                    await websocket.close(code=1002)
                    break
                if len(raw_frame.encode()) > _MAX_FRAME_BYTES:
                    await websocket.close(code=1009)
                    break
                try:
                    request = _load_json_document(raw_frame)
                except (UnicodeError, json.JSONDecodeError):
                    await websocket.close(code=1002)
                    break
                if not _within_json_limits(request):
                    await websocket.close(code=1009)
                    break
                if not isinstance(request, dict):
                    await websocket.close(code=1002)
                    break

                method = request.get("method")
                if method == "session.observe.subscribe":
                    parsed = (
                        _parse_subscribe_v2(request)
                        if observer_contract == 2
                        else _parse_subscribe(request)
                    )
                    if parsed is None or sessions is None:
                        await websocket.close(code=1002)
                        break
                    request_id, session_reference, profile, agent_id = parsed
                    if (
                        authenticated.agent_id is not None
                        and agent_id is not None
                        and authenticated.agent_id != agent_id
                    ):
                        await _send_error(
                            websocket,
                            request_id=request_id,
                            code=4001,
                            message="session not found",
                            lock=send_lock,
                        )
                        continue
                    selected_agent_id = authenticated.agent_id or agent_id
                    if observer_contract == 2:
                        try:
                            binding = await run_in_threadpool(
                                sessions.catalog_session_binding,
                                principal=principal,
                                session_id=UUID(session_reference),
                                agent_id=selected_agent_id,
                                profile=profile,
                            )
                        except (SessionNotFound, ValueError):
                            await _send_error(
                                websocket,
                                request_id=request_id,
                                code=4001,
                                message="session not found",
                                lock=send_lock,
                            )
                            continue
                        session_key = binding.session_key
                        profile = binding.profile
                        resolved_agent_id = binding.agent_id
                    else:
                        session_key = session_reference
                        try:
                            detail = await run_in_threadpool(
                                sessions.session_detail,
                                principal=principal,
                                session_key=session_key,
                                profile=profile,
                                **(
                                    {"agent_id": selected_agent_id}
                                    if selected_agent_id is not None
                                    else {}
                                ),
                            )
                        except SessionNotFound:
                            await _send_error(
                                websocket,
                                request_id=request_id,
                                code=4001,
                                message="session not found",
                                lock=send_lock,
                            )
                            continue
                        resolved_agent_id = _optional_agent_id(detail.get("agent_id"))
                    if resolved_agent_id is None or (
                        selected_agent_id is not None
                        and selected_agent_id != resolved_agent_id
                    ):
                        await _send_error(
                            websocket,
                            request_id=request_id,
                            code=4001,
                            message="session not found",
                            lock=send_lock,
                        )
                        continue
                    agent_id = resolved_agent_id
                    if event_task is not None:
                        task_to_cancel = event_task
                        event_task = None
                        if not await _cancel_forward_task(
                            task_to_cancel,
                            websocket,
                        ):
                            return
                    if active_handle is not None and subscription_manager is not None:
                        await run_in_threadpool(
                            subscription_manager.close_subscription,
                            principal=principal,
                            subscription_id=active_handle.subscription_id,
                            reason="subscription_replaced",
                        )
                        active_handle = None
                        active_subscription = None
                    handle = None
                    if subscription_manager is not None:
                        try:
                            handle = await run_in_threadpool(
                                subscription_manager.open_subscription,
                                principal=principal,
                                session_key=session_key,
                                profile=profile,
                                agent_id=agent_id,
                            )
                        except ObserverSubscriptionCapacityExceeded:
                            await _send_error(
                                websocket,
                                request_id=request_id,
                                code=4091,
                                message="projection replay is unavailable",
                                lock=send_lock,
                            )
                            continue
                        except (PermissionError, RuntimeError, TypeError, ValueError):
                            await _send_error(
                                websocket,
                                request_id=request_id,
                                code=4001,
                                message="session not found",
                                lock=send_lock,
                            )
                            continue
                    try:
                        snapshot = await _await_observer_snapshot(
                            sessions=sessions,
                            principal=principal,
                            session_key=session_key,
                            profile=(handle.profile if handle is not None else profile),
                            agent_id=agent_id,
                            subscription_manager=subscription_manager,
                            handle=handle,
                        )
                    except SessionNotFound:
                        if handle is not None and subscription_manager is not None:
                            await run_in_threadpool(
                                subscription_manager.close_subscription,
                                principal=principal,
                                subscription_id=handle.subscription_id,
                                reason="reconciliation",
                            )
                        await _send_error(
                            websocket,
                            request_id=request_id,
                            code=4001,
                            message="session not found",
                            lock=send_lock,
                        )
                        continue
                    active_handle = handle
                    active_subscription = (
                        str(handle.subscription_id)
                        if handle is not None
                        else str(uuid4())
                    )
                    result = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "subscription_id": active_subscription,
                            **snapshot,
                        },
                    }
                    if observer_contract == 2:
                        try:
                            require_cloud_frame("observe_subscribe_result", result)
                        except ObserverV2ContractError:
                            await websocket.close(code=1002)
                            break
                    if _json_size(result) > _MAX_FRAME_BYTES:
                        await _send_error(
                            websocket,
                            request_id=request_id,
                            code=4091,
                            message="projection replay is unavailable",
                            lock=send_lock,
                        )
                        continue
                    await _send_json(websocket, result, send_lock)
                    event_task = asyncio.create_task(
                        _forward_managed_events(
                            websocket=websocket,
                            source=source,
                            principal=principal,
                            session_key=session_key,
                            profile=(handle.profile if handle is not None else profile),
                            agent_id=agent_id,
                            runtime_session_id=str(
                                snapshot[
                                    "session_id"
                                    if observer_contract == 2
                                    else "runtime_session_id"
                                ]
                            ),
                            runtime_generation=(
                                str(snapshot["runtime_generation"])
                                if observer_contract == 2
                                else None
                            ),
                            after_sequence=int(snapshot["event_sequence"]),
                            observer_contract=observer_contract,
                            lock=send_lock,
                            subscription_manager=subscription_manager,
                            handle=handle,
                        )
                    )
                elif method == "session.observe.unsubscribe":
                    parsed = (
                        _parse_unsubscribe_v2(request)
                        if observer_contract == 2
                        else _parse_unsubscribe(request)
                    )
                    if parsed is None:
                        await websocket.close(code=1002)
                        break
                    request_id, subscription_id = parsed
                    if (
                        event_task is not None
                        and subscription_id == active_subscription
                    ):
                        task_to_cancel = event_task
                        event_task = None
                        active_subscription = None
                        if not await _cancel_forward_task(
                            task_to_cancel,
                            websocket,
                        ):
                            return
                        if (
                            active_handle is not None
                            and subscription_manager is not None
                        ):
                            await run_in_threadpool(
                                subscription_manager.close_subscription,
                                principal=principal,
                                subscription_id=active_handle.subscription_id,
                                reason="client_unsubscribe",
                            )
                        active_handle = None
                    unsubscribe_result = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": (
                            {"observer_contract": 2} if observer_contract == 2 else {}
                        ),
                    }
                    if observer_contract == 2:
                        require_cloud_frame(
                            "observe_unsubscribe_result",
                            unsubscribe_result,
                        )
                    await _send_json(websocket, unsubscribe_result, send_lock)
                else:
                    await websocket.close(code=1002)
                    break
        except (WebSocketDisconnect, _PeerSendFailed):
            pass
        finally:
            if event_task is not None:
                task_to_cancel = event_task
                event_task = None
                await _cancel_forward_task(task_to_cancel, websocket)
            if active_handle is not None and subscription_manager is not None:
                try:
                    await run_in_threadpool(
                        subscription_manager.close_subscription,
                        principal=principal,
                        subscription_id=active_handle.subscription_id,
                        reason="gateway_shutdown",
                    )
                except (PermissionError, RuntimeError, TypeError, ValueError):
                    pass

    application.add_api_websocket_route("/api/ws", observer_socket)


async def _serve_control_socket(
    websocket: WebSocket,
    authenticated: WebSocketTicketAuthentication,
    control_runtime: ControlRuntimePort | None,
) -> None:
    send_lock = asyncio.Lock()
    control_context = ControlRequestContext(
        authentication=authenticated,
        connection_id=str(uuid4()),
    )
    active_runtime = control_runtime
    runtime_opened = False
    close_reason = "client_disconnected"
    if active_runtime is not None:
        try:
            await active_runtime.open(context=control_context)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unavailable owner stays opaque
            active_runtime = None
        else:
            runtime_opened = True
    available_methods = (
        active_runtime.available_methods if active_runtime is not None else ()
    )
    error_codes = (
        dict(active_runtime.error_codes)
        if active_runtime is not None
        else _CONTROL_ERROR_CODES
    )
    try:
        await _send_json(
            websocket,
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "observer_contract": 1,
                        "control_contract": 1,
                        "connection_role": "control",
                        "control_available_methods": list(available_methods),
                        "control_error_codes": error_codes,
                    },
                },
            },
            send_lock,
        )
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                close_reason = "protocol_error"
                await websocket.close(code=1002)
                return
            raw_frame = message.get("text")
            if not isinstance(raw_frame, str):
                close_reason = "protocol_error"
                await websocket.close(code=1002)
                return
            if len(raw_frame.encode()) > _MAX_FRAME_BYTES:
                close_reason = "protocol_error"
                await websocket.close(code=1009)
                return
            try:
                request = _load_json_document(raw_frame)
            except (UnicodeError, json.JSONDecodeError):
                close_reason = "protocol_error"
                await websocket.close(code=1002)
                return
            if not _within_json_limits(request):
                close_reason = "protocol_error"
                await websocket.close(code=1009)
                return
            parsed = _parse_control_request(request)
            if parsed is None:
                close_reason = "protocol_error"
                await websocket.close(code=1002)
                return
            request_id, method, params = parsed
            if active_runtime is not None:
                if method not in available_methods:
                    await _send_error(
                        websocket,
                        request_id=request_id,
                        code=4209,
                        message="method not allowed for this control slice",
                        lock=send_lock,
                    )
                    continue
                try:
                    result = await active_runtime.execute(
                        context=control_context,
                        method=method,
                        params=params,
                    )
                except asyncio.CancelledError:
                    raise
                except ControlRpcError as error:
                    await _send_error(
                        websocket,
                        request_id=request_id,
                        code=error.code,
                        message=error.message,
                        lock=send_lock,
                    )
                    continue
                except Exception:  # noqa: BLE001 - runtime failures stay opaque
                    await _send_error(
                        websocket,
                        request_id=request_id,
                        code=4202,
                        message="authoritative live runtime unavailable",
                        lock=send_lock,
                    )
                    continue
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": dict(result),
                }
                if (
                    not _within_json_limits(response)
                    or _json_size(response) > _MAX_FRAME_BYTES
                ):
                    await _send_error(
                        websocket,
                        request_id=request_id,
                        code=4202,
                        message="authoritative live runtime unavailable",
                        lock=send_lock,
                    )
                    continue
                await _send_json(websocket, response, send_lock)
                continue
            if method.startswith("session.control.") or method == (
                "session.command.status"
            ):
                await _send_error(
                    websocket,
                    request_id=request_id,
                    code=4202,
                    message="authoritative live runtime unavailable",
                    lock=send_lock,
                )
                continue
            await _send_error(
                websocket,
                request_id=request_id,
                code=4209,
                message="method not allowed for this control slice",
                lock=send_lock,
            )
    except (WebSocketDisconnect, _PeerSendFailed):
        return
    finally:
        if runtime_opened and control_runtime is not None:
            try:
                await control_runtime.close(
                    context=control_context,
                    reason=close_reason,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - cleanup stays off wire/logs
                del error


def _parse_control_request(
    request: object,
) -> tuple[int, str, dict[str, object]] | None:
    if not isinstance(request, dict) or set(request) != {
        "jsonrpc",
        "id",
        "method",
        "params",
    }:
        return None
    request_id = request["id"]
    method = request["method"]
    params = request["params"]
    if (
        request["jsonrpc"] != "2.0"
        or not isinstance(request_id, int)
        or isinstance(request_id, bool)
        or request_id < 1
        or not isinstance(method, str)
        or not 1 <= len(method) <= 128
        or not isinstance(params, dict)
    ):
        return None
    return request_id, method, params


async def _receive_with_forward_supervision(
    websocket: WebSocket,
    event_task: asyncio.Task[None] | None,
) -> dict[str, Any]:
    receive_task = asyncio.create_task(websocket.receive())
    try:
        if event_task is None:
            return await receive_task
        if event_task.done():
            await event_task
            return await receive_task

        done, _pending = await asyncio.wait(
            {receive_task, event_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if event_task in done:
            await event_task
        return await receive_task
    finally:
        if not receive_task.done():
            receive_task.cancel()
            with suppress(asyncio.CancelledError):
                await receive_task


async def _cancel_forward_task(
    event_task: asyncio.Task[None],
    websocket: WebSocket,
) -> bool:
    if event_task.done():
        with suppress(asyncio.CancelledError, _PeerSendFailed, WebSocketDisconnect):
            await event_task
        return True

    event_task.cancel()
    done, _pending = await asyncio.wait(
        {event_task},
        timeout=_FORWARD_CANCEL_TIMEOUT_SECONDS,
    )
    if event_task not in done:
        event_task.add_done_callback(_observe_task_result)
        await _close_unresponsive_forward(websocket)
        return False

    with suppress(asyncio.CancelledError, _PeerSendFailed, WebSocketDisconnect):
        await event_task
    return True


async def _close_unresponsive_forward(websocket: WebSocket) -> None:
    close_task = asyncio.create_task(websocket.close(code=1011))
    done, _pending = await asyncio.wait(
        {close_task},
        timeout=_FORWARD_CANCEL_TIMEOUT_SECONDS,
    )
    if close_task not in done:
        close_task.cancel()
        close_task.add_done_callback(_observe_task_result)
        return
    with suppress(WebSocketDisconnect, OSError, RuntimeError):
        await close_task


def _observe_task_result(task: asyncio.Task[Any]) -> None:
    with suppress(asyncio.CancelledError):
        task.exception()


async def _await_observer_snapshot(
    *,
    sessions: SessionQueryService,
    principal: Principal,
    session_key: str,
    profile: str | None,
    agent_id: UUID | None,
    subscription_manager: ObserverSubscriptionPort | None,
    handle: ObserverSubscription | None,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + _INITIAL_SNAPSHOT_TIMEOUT_SECONDS
    while True:
        ready = handle is None or not handle.requires_initial_snapshot
        if not ready and subscription_manager is not None and handle is not None:
            ready = await run_in_threadpool(
                subscription_manager.snapshot_ready,
                principal=principal,
                subscription_id=handle.subscription_id,
            )
        if ready:
            try:
                return await run_in_threadpool(
                    sessions.observer_snapshot,
                    principal=principal,
                    session_key=session_key,
                    profile=profile,
                    agent_id=agent_id,
                )
            except SessionNotFound:
                if subscription_manager is None:
                    raise
        if asyncio.get_running_loop().time() >= deadline:
            raise SessionNotFound
        await asyncio.sleep(_INITIAL_SNAPSHOT_POLL_SECONDS)


async def _forward_managed_events(
    *,
    websocket: WebSocket,
    source: ProjectionEventSourcePort,
    principal: Principal,
    session_key: str,
    profile: str | None,
    agent_id: UUID | None,
    runtime_session_id: str,
    runtime_generation: str | None = None,
    after_sequence: int,
    observer_contract: int = 1,
    lock: asyncio.Lock,
    subscription_manager: ObserverSubscriptionPort | None,
    handle: ObserverSubscription | None,
) -> None:
    forward = asyncio.create_task(
        _forward_events(
            websocket=websocket,
            source=source,
            principal=principal,
            session_key=session_key,
            profile=profile,
            agent_id=agent_id,
            runtime_session_id=runtime_session_id,
            runtime_generation=runtime_generation,
            after_sequence=after_sequence,
            observer_contract=observer_contract,
            lock=lock,
        )
    )
    if subscription_manager is None or handle is None:
        await forward
        return

    async def renew() -> None:
        while True:
            await asyncio.sleep(_LEASE_RENEW_INTERVAL_SECONDS)
            await run_in_threadpool(
                subscription_manager.renew_subscription,
                principal=principal,
                subscription_id=handle.subscription_id,
            )

    renewal = asyncio.create_task(renew())
    tasks = {forward, renewal}
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


async def _forward_events(
    *,
    websocket: WebSocket,
    source: ProjectionEventSourcePort,
    principal: Principal,
    session_key: str,
    profile: str | None,
    agent_id: UUID | None = None,
    runtime_session_id: str,
    runtime_generation: str | None = None,
    after_sequence: int,
    observer_contract: int = 1,
    lock: asyncio.Lock,
) -> None:
    last_sequence = after_sequence
    try:
        event_scope: dict[str, object] = {}
        if agent_id is not None:
            event_scope["agent_id"] = agent_id
        async for event in source.events(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_key=session_key,
            profile=profile,
            after_sequence=after_sequence,
            **event_scope,
        ):
            if observer_contract == 2:
                outbound = {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": dict(event),
                }
                try:
                    require_cloud_frame("session_event", outbound)
                except ObserverV2ContractError:
                    await websocket.close(code=1002)
                    return
                if (
                    event.get("observer_contract") != 2
                    or event.get("profile") != profile
                    or event.get("runtime_generation") != runtime_generation
                ):
                    await websocket.close(code=1002)
                    return
            elif not _valid_event(event):
                continue
            event_sequence = int(event["event_sequence"])
            if event_sequence <= last_sequence:
                continue
            event_sequence_start = int(
                event.get("event_sequence_start", event_sequence)
            )
            identity_matches = event["session_id"] == runtime_session_id and (
                observer_contract == 2 or event["session_key"] == session_key
            )
            if (
                event_sequence_start != last_sequence + 1
                or not identity_matches
            ):
                await _send_error(
                    websocket,
                    request_id=None,
                    code=4091,
                    message="projection replay is unavailable",
                    lock=lock,
                )
                return
            outbound = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": dict(event),
            }
            if _json_size(outbound) > _MAX_FRAME_BYTES:
                await _send_error(
                    websocket,
                    request_id=None,
                    code=4091,
                    message="projection replay is unavailable",
                    lock=lock,
                )
                return
            await _send_json(websocket, outbound, lock)
            last_sequence = event_sequence
    except asyncio.CancelledError:
        raise
    except _PeerSendFailed:
        raise
    except Exception:  # noqa: BLE001 - keep event-source failures off the wire
        await _send_error(
            websocket,
            request_id=None,
            code=4091,
            message="projection replay is unavailable",
            lock=lock,
        )


def _parse_subscribe(
    request: dict[str, Any],
) -> tuple[int, str, str | None, UUID | None] | None:
    if set(request) != {"jsonrpc", "id", "method", "params"}:
        return None
    if request["jsonrpc"] != "2.0":
        return None
    request_id = request["id"]
    params = request["params"]
    if (
        not isinstance(request_id, int)
        or isinstance(request_id, bool)
        or request_id < 1
        or not isinstance(params, dict)
        or not {"session_key"} <= set(params) <= {
            "session_key",
            "profile",
            "agent_id",
        }
    ):
        return None
    session_key = params["session_key"]
    profile = params.get("profile")
    agent_id = _optional_agent_id(params.get("agent_id"))
    if (
        not isinstance(session_key, str)
        or not 1 <= len(session_key) <= 256
        or (
            profile is not None
            and (not isinstance(profile, str) or not 1 <= len(profile) <= 128)
        )
        or ("agent_id" in params and agent_id is None)
    ):
        return None
    return request_id, session_key, profile, agent_id


def _parse_subscribe_v2(
    request: dict[str, Any],
) -> tuple[int, str, str, UUID | None] | None:
    try:
        require_cloud_frame("observe_subscribe_request", request)
    except ObserverV2ContractError:
        return None
    params = request["params"]
    assert isinstance(params, dict)
    agent_id = _optional_agent_id(params.get("agent_id"))
    if "agent_id" in params and agent_id is None:
        return None
    return int(request["id"]), str(params["session_id"]), str(params["profile"]), agent_id


def _optional_agent_id(value: object) -> UUID | None:
    if value is None:
        return None
    if not is_canonical_rfc4122_uuid_v1_to_v5(value):
        return None
    assert isinstance(value, str)
    return UUID(value)


def _parse_unsubscribe(
    request: dict[str, Any],
) -> tuple[int, str] | None:
    if set(request) != {"jsonrpc", "id", "method", "params"}:
        return None
    if request["jsonrpc"] != "2.0":
        return None
    request_id = request["id"]
    params = request["params"]
    if (
        not isinstance(request_id, int)
        or isinstance(request_id, bool)
        or request_id < 1
        or not isinstance(params, dict)
        or set(params) != {"subscription_id"}
    ):
        return None
    subscription_id = params["subscription_id"]
    if not isinstance(subscription_id, str) or not 1 <= len(subscription_id) <= 256:
        return None
    return request_id, subscription_id


def _parse_unsubscribe_v2(
    request: dict[str, Any],
) -> tuple[int, str] | None:
    try:
        require_cloud_frame("observe_unsubscribe_request", request)
    except ObserverV2ContractError:
        return None
    params = request["params"]
    assert isinstance(params, dict)
    return int(request["id"]), str(params["subscription_id"])


def _within_json_limits(value: object, depth: int = 1) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, str):
        return len(value.encode()) <= _MAX_STRING_BYTES
    if isinstance(value, dict):
        return len(value) <= _MAX_FIELDS and all(
            isinstance(key, str)
            and len(key.encode()) <= _MAX_STRING_BYTES
            and _within_json_limits(item, depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return len(value) <= _MAX_ITEMS and all(
            _within_json_limits(item, depth + 1) for item in value
        )
    if value is None or isinstance(value, (bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _load_json_document(raw_frame: str) -> object:
    def reject_constant(value: str) -> object:
        raise json.JSONDecodeError(
            f"non-finite JSON number is forbidden: {value}",
            raw_frame,
            0,
        )

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise json.JSONDecodeError(
                    f"duplicate JSON object key is forbidden: {key}",
                    raw_frame,
                    0,
                )
            result[key] = value
        return result

    return json.loads(
        raw_frame,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _valid_event(event: Mapping[str, object]) -> bool:
    required = {
        "type",
        "session_id",
        "session_key",
        "event_sequence",
        "payload",
    }
    if not required <= set(event) <= required | {"event_sequence_start"}:
        return False
    event_type = event["type"]
    event_sequence = event["event_sequence"]
    if (
        event_type not in _EVENT_TYPES
        or not isinstance(event["session_id"], str)
        or not 1 <= len(event["session_id"]) <= 256
        or not isinstance(event["session_key"], str)
        or not 1 <= len(event["session_key"]) <= 256
        or not isinstance(event_sequence, int)
        or isinstance(event_sequence, bool)
        or event_sequence < 1
        or not isinstance(event["payload"], dict)
        or not _within_json_limits(event["payload"])
    ):
        return False
    sequence_start = event.get("event_sequence_start")
    if sequence_start is not None and (
        event_type not in _MERGEABLE_EVENT_TYPES
        or not isinstance(sequence_start, int)
        or isinstance(sequence_start, bool)
        or not 1 <= sequence_start <= event_sequence
    ):
        return False
    return _valid_event_payload(event_type, event["payload"])


def _valid_event_payload(event_type: object, payload: dict[object, object]) -> bool:
    if event_type in {
        "message.delta",
        "reasoning.delta",
        "thinking.delta",
    }:
        return set(payload) == {"text"} and _bounded_string(
            payload["text"],
            maximum_bytes=_MAX_STRING_BYTES,
        )
    if event_type == "status.update":
        if (
            not {"status", "running"}
            <= set(payload)
            <= {
                "status",
                "running",
                "text",
            }
        ):
            return False
        status = payload["status"]
        running = payload["running"]
        return (
            isinstance(status, str)
            and 1 <= len(status) <= 64
            and isinstance(running, bool)
            and running == (status in _RUNNING_STATUSES)
            and (
                "text" not in payload
                or _bounded_string(
                    payload["text"],
                    maximum_bytes=_MAX_STRING_BYTES,
                )
            )
        )
    if event_type == "message.start":
        return set(payload) <= {"message_id", "role"} and all(
            (key != "message_id" or _bounded_string(value, minimum=1, maximum=256))
            and (key != "role" or value == "assistant")
            for key, value in payload.items()
        )
    if event_type == "message.complete":
        if not {"status"} <= set(payload) <= {"text", "status", "error"}:
            return False
        return (
            payload["status"] in {"complete", "error"}
            and (
                "text" not in payload
                or _bounded_string(
                    payload["text"],
                    maximum_bytes=_MAX_STRING_BYTES,
                )
            )
            and (
                "error" not in payload
                or payload["error"] is None
                or _bounded_string(payload["error"], maximum=4096)
            )
        )
    if event_type in {"agent.terminal.output", "tool.output.delta"}:
        allowed = (
            {"process_id", "stream", "text", "sequence"}
            if event_type == "agent.terminal.output"
            else {"tool_call_id", "tool_name", "text", "sequence"}
        )
        if "text" not in payload or not set(payload) <= allowed:
            return False
        if not _bounded_string(payload["text"], maximum_bytes=_MAX_STRING_BYTES):
            return False
        for key, value in payload.items():
            if key in {"process_id", "tool_call_id", "tool_name"} and not (
                _bounded_string(value, minimum=1, maximum=256)
            ):
                return False
            if key == "stream" and value not in {"stdout", "stderr"}:
                return False
            if key == "sequence" and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                return False
        return True
    return False


def _bounded_string(
    value: object,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    maximum_bytes: int | None = None,
) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= minimum
        and (maximum is None or len(value) <= maximum)
        and (maximum_bytes is None or len(value.encode()) <= maximum_bytes)
    )


async def _send_error(
    websocket: WebSocket,
    *,
    request_id: int | None,
    code: int,
    message: str,
    lock: asyncio.Lock,
) -> None:
    await _send_json(
        websocket,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        lock,
    )


async def _send_json(
    websocket: WebSocket,
    payload: dict[str, object],
    lock: asyncio.Lock,
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        async with lock:
            await asyncio.wait_for(
                websocket.send_text(encoded),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
    except asyncio.CancelledError:
        raise
    except (TimeoutError, WebSocketDisconnect, OSError, RuntimeError) as error:
        raise _PeerSendFailed("peer send failed") from error


def _json_size(payload: dict[str, object]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
