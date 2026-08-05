"""FastAPI routes for authenticated session projection reads."""

from __future__ import annotations

import json
import math
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.adapters.http_auth import (
    authenticate_catalog_request,
)
from hermes_cloud.modules.cloud_api.application.service import CloudApiService
from hermes_cloud.modules.cloud_api.application.sessions import (
    SessionNotFound,
    SessionQueryService,
    SessionScopeAmbiguous,
)
from hermes_cloud.modules.cloud_api.domain import (
    is_canonical_rfc4122_uuid_v1_to_v5,
)

_SESSION_PAGE_MAX_BYTES = 256 * 1024
_TRANSCRIPT_MAX_BYTES = 4 * 1024 * 1024
_MAX_STRING_BYTES = 128 * 1024
_MAX_DEPTH = 32
_MAX_FIELDS = 1024
_MAX_ITEMS = 1024
_SESSION_LIST_QUERY_KEYS = frozenset(
    {"limit", "offset", "min_messages", "archived", "order", "profile"}
)


def register_session_routes(
    application: FastAPI,
    *,
    authentication: CloudApiService,
    sessions: SessionQueryService,
) -> None:
    async def _list_sessions(
        request: Request,
        *,
        agent_id: UUID | None,
    ) -> JSONResponse:
        principal = await authenticate_catalog_request(request, authentication)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            limit = _bounded_integer(request, "limit", default=20, minimum=1)
            offset = _bounded_integer(
                request,
                "offset",
                default=0,
                minimum=0,
                maximum=None,
            )
            min_messages = _bounded_integer(
                request,
                "min_messages",
                default=1,
                minimum=1,
                maximum=1,
            )
            profile = _bounded_profile(request)
            if request.query_params.get("archived", "exclude") != "exclude":
                return _invalid_request()
            if request.query_params.get("order", "recent") != "recent":
                return _invalid_request()
        except ValueError:
            return _invalid_request()
        try:
            payload = await run_in_threadpool(
                sessions.list_sessions,
                principal=principal,
                limit=limit,
                offset=offset,
                min_messages=min_messages,
                agent_id=agent_id,
                profile=profile,
            )
        except SessionScopeAmbiguous:
            return _session_scope_ambiguous()
        return _bounded_response(payload, _SESSION_PAGE_MAX_BYTES)

    async def list_sessions(request: Request) -> JSONResponse:
        try:
            _require_unique_query(request, _SESSION_LIST_QUERY_KEYS | {"agent_id"})
            agent_id = _optional_uuid_query(request, "agent_id")
        except ValueError:
            return _invalid_request()
        return await _list_sessions(request, agent_id=agent_id)

    async def list_agent_sessions(
        agent_id: str,
        request: Request,
    ) -> JSONResponse:
        if (
            not is_canonical_rfc4122_uuid_v1_to_v5(agent_id)
        ):
            return _invalid_request()
        try:
            _require_unique_query(request, _SESSION_LIST_QUERY_KEYS)
            principal = await authenticate_catalog_request(request, authentication)
            if isinstance(principal, JSONResponse):
                return principal
            limit = _bounded_integer(request, "limit", default=20, minimum=1)
            offset = _bounded_integer(
                request, "offset", default=0, minimum=0, maximum=None
            )
            min_messages = _bounded_integer(
                request, "min_messages", default=0, minimum=0, maximum=1
            )
            profile = _bounded_profile(request)
            if request.query_params.get("archived", "exclude") != "exclude":
                return _invalid_request()
            if request.query_params.get("order", "recent") != "recent":
                return _invalid_request()
        except ValueError:
            return _invalid_request()
        payload = await run_in_threadpool(
            sessions.list_agent_catalog_sessions,
            principal=principal,
            agent_id=UUID(agent_id),
            limit=limit,
            offset=offset,
            min_messages=min_messages,
            profile=profile,
        )
        return _bounded_response(payload, _SESSION_PAGE_MAX_BYTES)

    async def session_detail(
        session_id: str,
        request: Request,
        agent_id: str | None = None,
    ) -> JSONResponse:
        principal = await authenticate_catalog_request(request, authentication)
        if isinstance(principal, JSONResponse):
            return principal
        if not is_canonical_rfc4122_uuid_v1_to_v5(session_id):
            return _invalid_request()
        path_agent_id = request.path_params.get("agent_id")
        try:
            _require_unique_query(
                request,
                frozenset(
                    {"profile"}
                    if path_agent_id is not None
                    else {"profile", "agent_id"}
                ),
            )
            profile = _bounded_profile(request)
            if path_agent_id is not None:
                if not is_canonical_rfc4122_uuid_v1_to_v5(path_agent_id):
                    raise ValueError
                resolved_agent_id = UUID(path_agent_id)
            else:
                resolved_agent_id = _optional_uuid_query(request, "agent_id")
        except ValueError:
            return _invalid_request()
        if resolved_agent_id is None:
            return _invalid_request()
        try:
            payload = await run_in_threadpool(
                sessions.catalog_session_detail,
                principal=principal,
                session_id=UUID(session_id),
                agent_id=resolved_agent_id,
                profile=profile,
            )
        except SessionNotFound:
            return _session_not_found()
        return _bounded_response(payload, _SESSION_PAGE_MAX_BYTES)

    async def session_messages(
        session_id: str,
        request: Request,
        agent_id: str | None = None,
    ) -> JSONResponse:
        principal = await authenticate_catalog_request(request, authentication)
        if isinstance(principal, JSONResponse):
            return principal
        if not is_canonical_rfc4122_uuid_v1_to_v5(session_id):
            return _invalid_request()
        path_agent_id = request.path_params.get("agent_id")
        try:
            _require_unique_query(
                request,
                frozenset(
                    {"profile", "limit", "offset"}
                    if path_agent_id is not None
                    else {"profile", "limit", "offset", "agent_id"}
                ),
            )
            profile = _bounded_profile(request)
            if path_agent_id is not None:
                if not is_canonical_rfc4122_uuid_v1_to_v5(path_agent_id):
                    raise ValueError
                resolved_agent_id = UUID(path_agent_id)
            else:
                resolved_agent_id = _optional_uuid_query(request, "agent_id")
            if resolved_agent_id is None:
                raise ValueError
            limit = _bounded_integer(request, "limit", default=200, minimum=1)
            offset = _bounded_integer(
                request,
                "offset",
                default=0,
                minimum=0,
                maximum=None,
            )
            payload = await run_in_threadpool(
                sessions.catalog_session_messages,
                principal=principal,
                session_id=UUID(session_id),
                limit=limit,
                offset=offset,
                agent_id=resolved_agent_id,
                profile=profile,
            )
        except ValueError:
            return _invalid_request()
        except SessionNotFound:
            return _session_not_found()
        return _bounded_response(payload, _TRANSCRIPT_MAX_BYTES)

    async def list_agents(request: Request) -> JSONResponse:
        principal = await authenticate_catalog_request(request, authentication)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            workspace_id = _optional_uuid_query(request, "workspace_id")
            if set(request.query_params) - {"workspace_id"}:
                raise ValueError
        except ValueError:
            return _invalid_request()
        payload = await run_in_threadpool(
            sessions.list_agents,
            principal=principal,
            workspace_id=workspace_id,
        )
        return _bounded_response(payload, _SESSION_PAGE_MAX_BYTES)

    application.add_api_route(
        "/api/agents",
        list_agents,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/v1/agents",
        list_agents,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/sessions",
        list_sessions,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/v1/agents/{agent_id}/sessions",
        list_agent_sessions,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/sessions/{session_id}",
        session_detail,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/sessions/{session_id}/messages",
        session_messages,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/v1/agents/{agent_id}/sessions/{session_id}",
        session_detail,
        methods=["GET"],
    )
    application.add_api_route(
        "/api/v1/agents/{agent_id}/sessions/{session_id}/messages",
        session_messages,
        methods=["GET"],
    )


def _bounded_integer(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = 500,
) -> int:
    raw = request.query_params.get(name)
    value = default if raw is None else int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError
    return value


def _optional_uuid_query(request: Request, name: str) -> UUID | None:
    values = request.query_params.getlist(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError
    value = values[0]
    if not is_canonical_rfc4122_uuid_v1_to_v5(value):
        raise ValueError
    return UUID(value)


def _require_unique_query(request: Request, allowed: frozenset[str]) -> None:
    keys = set(request.query_params)
    if keys - allowed or any(len(request.query_params.getlist(key)) != 1 for key in keys):
        raise ValueError


def _bounded_response(
    payload: dict[str, object],
    maximum_bytes: int,
) -> JSONResponse:
    if not _within_json_limits(payload):
        return _response_too_large()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if len(encoded) > maximum_bytes:
        return _response_too_large()
    return JSONResponse(payload)


def _bounded_profile(request: Request) -> str | None:
    values = request.query_params.getlist("profile")
    if len(values) > 1:
        raise ValueError
    if values and not _bounded_text(values[0], minimum=1, maximum=128):
        raise ValueError
    return values[0] if values else None


def _bounded_text(value: object, *, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum


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
    if isinstance(value, (list, tuple)):
        return len(value) <= _MAX_ITEMS and all(
            _within_json_limits(item, depth + 1) for item in value
        )
    if value is None or isinstance(value, (bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _response_too_large() -> JSONResponse:
    return JSONResponse(
        {
            "code": "RESPONSE_TOO_LARGE",
            "reason": "response exceeds contract limit",
        },
        status_code=413,
    )


def _invalid_request() -> JSONResponse:
    return JSONResponse(
        {
            "code": "INVALID_REQUEST",
            "reason": "request parameters are invalid",
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


def _session_scope_ambiguous() -> JSONResponse:
    return JSONResponse(
        {
            "code": "SESSION_SCOPE_AMBIGUOUS",
            "reason": "session scope is ambiguous",
        },
        status_code=409,
    )
