"""Authenticated HTTP adapter for Hermes Desktop update checks.

The route is intentionally thin: bearer authentication establishes tenant identity,
strict JSON becomes a DeviceUpdateContextV1, and all rollout/update decisions stay in
UpdateCheckService.  No OSS credentials or storage SDK types cross this boundary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.adapters.http_auth import authenticate_bearer
from hermes_cloud.modules.cloud_api.application.service import CloudApiService

from .domain import DeviceUpdateContextV1, UpdateDecisionV1
from .service import UpdateCheckPolicyError, UpdateCheckService

_MAX_BODY_BYTES = 32 * 1024
_ALLOWED_FIELDS = frozenset(
    {
        "device_id",
        "target",
        "os_version",
        "active_release_id",
        "active_release_generation",
        "highest_release_generation",
        "requested_channel",
        "enterprise_pin_release_id",
    }
)


def register_update_check_route(
    application: FastAPI,
    *,
    authentication: CloudApiService,
    updates: UpdateCheckService,
) -> None:
    async def update_check(request: Request) -> JSONResponse:
        principal = await authenticate_bearer(request, authentication)
        if isinstance(principal, JSONResponse):
            return principal
        try:
            body = await _strict_json_object(request)
            context = DeviceUpdateContextV1(
                device_id=_required_string(body, "device_id"),
                organization_id=str(principal.tenant_id),
                target=_required_string(body, "target"),
                os_version=_required_string(body, "os_version"),
                active_release_id=_optional_string(body, "active_release_id"),
                active_release_generation=_required_generation(
                    body, "active_release_generation"
                ),
                highest_release_generation=_required_generation(
                    body, "highest_release_generation"
                ),
                requested_channel=_required_string(body, "requested_channel"),
                enterprise_pin_release_id=_optional_string(
                    body, "enterprise_pin_release_id"
                ),
            )
            decision = await run_in_threadpool(updates.check, context)
        except (UpdateCheckPolicyError, TypeError, ValueError):
            return _invalid_request()
        return JSONResponse(_decision_payload(decision))

    application.add_api_route(
        "/api/desktop/update-check",
        update_check,
        methods=["POST"],
    )


async def _strict_json_object(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise ValueError("application/json required")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if declared < 0 or declared > _MAX_BODY_BYTES:
            raise ValueError("request body too large")
    payload = await request.body()
    if not payload or len(payload) > _MAX_BODY_BYTES:
        raise ValueError("request body size invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = value
        return output

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != _ALLOWED_FIELDS:
        raise ValueError("update-check fields are invalid")
    return value


def _required_string(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be null or non-empty string")
    return value


def _required_generation(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _decision_payload(decision: UpdateDecisionV1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": decision.status.value,
        "reason_code": decision.reason_code,
        "release_id": decision.release_id,
        "product_version": decision.product_version,
        "release_generation": decision.release_generation,
        "channel": decision.channel,
        "rollout_bucket": decision.rollout_bucket,
        "mandatory": decision.mandatory,
        "release_envelope": _mapping_or_none(decision.release_envelope),
        "channel_envelope": _mapping_or_none(decision.channel_envelope),
        "block_envelope": _mapping_or_none(decision.block_envelope),
        "download_grants": [
            {
                "object_key": grant.object_key,
                "sha256": grant.sha256,
                "size_bytes": grant.size_bytes,
                "url": grant.url,
                "expires_at": _rfc3339(grant.expires_at),
            }
            for grant in decision.download_grants
        ],
    }


def _mapping_or_none(value: object) -> object:
    return None if value is None else dict(value)  # type: ignore[arg-type]


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise UpdateCheckPolicyError("grant expiry must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _invalid_request() -> JSONResponse:
    return JSONResponse(
        {
            "code": "INVALID_UPDATE_CHECK_REQUEST",
            "reason": "invalid update-check request",
        },
        status_code=400,
    )
