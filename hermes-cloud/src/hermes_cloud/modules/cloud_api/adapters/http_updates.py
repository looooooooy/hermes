"""Authenticated desktop software update-check HTTP adapter.

The route is deliberately thin: it authenticates the native Bearer token, parses a
bounded strict request, derives organization scope from the authenticated principal,
and delegates all rollout / block / pin / grant decisions to UpdateCheckService.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.adapters.http_auth import authenticate_bearer
from hermes_cloud.modules.cloud_api.application.service import CloudApiService
from hermes_cloud.modules.cloud_api.domain import is_canonical_rfc4122_uuid_v1_to_v5
from hermes_cloud.modules.release_update import (
    DeviceUpdateContextV1,
    UpdateCheckPolicyError,
    UpdateCheckService,
    UpdateDecisionV1,
)

_MAX_UPDATE_CHECK_BYTES = 64 * 1024
_ALLOWED_FIELDS = {
    "device_id",
    "target",
    "os_version",
    "active_release_id",
    "active_release_generation",
    "highest_release_generation",
    "requested_channel",
}
_ALLOWED_TARGETS = {
    "macos-aarch64",
    "macos-x86_64",
    "windows-x86_64",
    "linux-x86_64",
    "linux-aarch64",
}
_ALLOWED_CHANNELS = {"canary", "beta", "stable", "enterprise"}


def register_desktop_update_routes(
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
            context = _device_context(body, organization_id=str(principal.tenant_id))
            decision = await run_in_threadpool(updates.check, context)
        except (ValueError, TypeError, UpdateCheckPolicyError):
            return JSONResponse(
                {"code": "UPDATE_CHECK_UNAVAILABLE", "reason": "update check rejected"},
                status_code=503,
            )
        return JSONResponse(_decision_json(decision))

    application.add_api_route(
        "/api/desktop/update-check",
        update_check,
        methods=["POST"],
    )


async def _strict_json_object(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("content type must be application/json")
    raw = await request.body()
    if not raw or len(raw) > _MAX_UPDATE_CHECK_BYTES:
        raise ValueError("body is empty or oversized")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != _ALLOWED_FIELDS:
        raise ValueError("update-check fields are invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _device_context(
    body: Mapping[str, object],
    *,
    organization_id: str,
) -> DeviceUpdateContextV1:
    device_id = body["device_id"]
    target = body["target"]
    os_version = body["os_version"]
    active_release_id = body["active_release_id"]
    active_generation = body["active_release_generation"]
    highest_generation = body["highest_release_generation"]
    requested_channel = body["requested_channel"]

    if not isinstance(device_id, str) or not is_canonical_rfc4122_uuid_v1_to_v5(device_id):
        raise ValueError("invalid device_id")
    if not isinstance(target, str) or target not in _ALLOWED_TARGETS:
        raise ValueError("invalid target")
    if not isinstance(os_version, str) or not 1 <= len(os_version) <= 128:
        raise ValueError("invalid os_version")
    if active_release_id is not None and (
        not isinstance(active_release_id, str) or not 1 <= len(active_release_id) <= 160
    ):
        raise ValueError("invalid active_release_id")
    if type(active_generation) is not int or active_generation < 0:
        raise ValueError("invalid active_release_generation")
    if type(highest_generation) is not int or highest_generation < active_generation:
        raise ValueError("invalid highest_release_generation")
    if not isinstance(requested_channel, str) or requested_channel not in _ALLOWED_CHANNELS:
        raise ValueError("invalid requested_channel")

    return DeviceUpdateContextV1(
        device_id=device_id,
        organization_id=organization_id,
        target=target,
        os_version=os_version,
        active_release_id=active_release_id,
        active_release_generation=active_generation,
        highest_release_generation=highest_generation,
        requested_channel=requested_channel,
        enterprise_pin_release_id=None,
    )


def _decision_json(decision: UpdateDecisionV1) -> dict[str, object]:
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
                "expires_at": grant.expires_at.isoformat().replace("+00:00", "Z"),
            }
            for grant in decision.download_grants
        ],
    }


def _mapping_or_none(value: Mapping[str, object] | None) -> dict[str, object] | None:
    return None if value is None else dict(value)
