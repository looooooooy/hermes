"""FastAPI route for minting single-use observer and control tickets."""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.adapters.http_auth import (
    authenticate_ticket_request,
)
from hermes_cloud.modules.cloud_api.application.service import CloudApiService
from hermes_cloud.modules.cloud_api.application.sessions import (
    SessionNotFound,
    SessionQueryService,
)
from hermes_cloud.modules.cloud_api.domain import (
    is_canonical_rfc4122_uuid_v1_to_v5,
)


def register_ticket_route(
    application: FastAPI,
    *,
    service: CloudApiService,
    sessions: SessionQueryService | None,
) -> None:
    async def mint_ticket(request: Request) -> JSONResponse:
        principal = await authenticate_ticket_request(request, service)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            body = await request.json()
        except ValueError:
            return _invalid_request()
        if body == {}:
            issued = await run_in_threadpool(service.mint_observer_ticket, principal)
            return JSONResponse(
                {
                    "ticket": issued.ticket.reveal(),
                    "ttl_seconds": issued.ttl_seconds,
                    "connection_role": "observer",
                }
            )
        observer_request = _parse_observer_request(body)
        if observer_request is not None:
            observer_client_id, observer_contract, observer_agent_id = observer_request
            issued = await run_in_threadpool(
                service.mint_observer_ticket,
                principal,
                client_instance_id=observer_client_id,
                observer_contract=observer_contract,
                agent_id=observer_agent_id,
            )
            return JSONResponse(
                {
                    "ticket": issued.ticket.reveal(),
                    "ttl_seconds": issued.ttl_seconds,
                    "connection_role": "observer",
                    **({"observer_contract": 2} if observer_contract == 2 else {}),
                }
            )
        parsed = _parse_control_request(body)
        if parsed is None:
            return _invalid_request()
        client_instance_id, session_id, agent_id = parsed
        if sessions is None:
            return _session_not_found()
        try:
            binding = await run_in_threadpool(
                sessions.catalog_session_binding,
                principal=principal,
                session_id=session_id,
                agent_id=agent_id,
                profile=None,
            )
        except SessionNotFound:
            return _session_not_found()
        if binding.session_id != session_id or binding.agent_id != agent_id:
            return _session_not_found()
        issued = await run_in_threadpool(
            service.mint_control_ticket,
            principal,
            client_instance_id=client_instance_id,
            session_id=binding.session_id,
            profile=binding.profile,
            agent_id=binding.agent_id,
        )
        return JSONResponse(
            {
                "ticket": issued.ticket.reveal(),
                "ttl_seconds": issued.ttl_seconds,
                "connection_role": "control",
            }
        )

    application.add_api_route(
        "/api/auth/ws-ticket",
        mint_ticket,
        methods=["POST"],
    )


def _invalid_request() -> JSONResponse:
    return JSONResponse(
        {
            "code": "INVALID_REQUEST",
            "reason": "request body is invalid",
        },
        status_code=400,
    )


def _session_not_found() -> JSONResponse:
    return JSONResponse(
        {
            "code": "SESSION_NOT_FOUND",
            "reason": "session not found",
        },
        status_code=404,
    )


def _parse_control_request(body: object) -> tuple[str, UUID, UUID] | None:
    if not isinstance(body, dict) or set(body) != {
        "connection_role",
        "client_instance_id",
        "session_id",
        "agent_id",
    }:
        return None
    client_instance_id = body["client_instance_id"]
    session_id = body["session_id"]
    agent_id = body["agent_id"]
    if (
        body["connection_role"] != "control"
        or not is_canonical_rfc4122_uuid_v1_to_v5(client_instance_id)
        or not is_canonical_rfc4122_uuid_v1_to_v5(session_id)
        or not is_canonical_rfc4122_uuid_v1_to_v5(agent_id)
    ):
        return None
    return (
        client_instance_id,
        UUID(session_id),
        UUID(agent_id),
    )


def _parse_observer_request(body: object) -> tuple[str, int, UUID | None] | None:
    if not isinstance(body, dict) or set(body) not in (
        {"connection_role", "client_instance_id"},
        {"connection_role", "client_instance_id", "agent_id"},
        {"connection_role", "client_instance_id", "observer_contract"},
        {
            "connection_role",
            "client_instance_id",
            "observer_contract",
            "agent_id",
        },
    ):
        return None
    client_instance_id = body["client_instance_id"]
    if body["connection_role"] != "observer" or not is_canonical_rfc4122_uuid_v1_to_v5(
        client_instance_id
    ):
        return None
    observer_contract = body.get("observer_contract", 1)
    if observer_contract not in {1, 2} or isinstance(observer_contract, bool):
        return None
    agent_id = body.get("agent_id")
    if agent_id is not None and not is_canonical_rfc4122_uuid_v1_to_v5(agent_id):
        return None
    return (
        client_instance_id,
        observer_contract,
        UUID(agent_id) if agent_id is not None else None,
    )

