"""Authenticated read-only onboarding context for Desktop pairing."""

from __future__ import annotations

from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.application.service import AuthenticationFailed
from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.device.onboarding_context import PairingContextResolverPort


class AccessAuthenticator(Protocol):
    def authenticate_access(self, token: str) -> Principal: ...


def register_pairing_context_route(
    application: FastAPI,
    *,
    authentication: AccessAuthenticator,
    resolver: PairingContextResolverPort,
) -> None:
    async def pairing_context(request: Request) -> JSONResponse:
        principal = _owner(request, authentication)
        if principal is None:
            return JSONResponse(
                {"code": "UNAUTHORIZED", "reason": "authentication required"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        try:
            targets = await run_in_threadpool(resolver.targets_for, principal)
        except (TypeError, ValueError):
            return JSONResponse(
                {"code": "PAIRING_CONTEXT_UNAVAILABLE", "reason": "pairing context unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "targets": [
                    {
                        "workspace_id": target.workspace_id,
                        "workspace_key": target.workspace_key,
                        "workspace_display_name": target.workspace_display_name,
                        "agent_id": target.agent_id,
                        "agent_key": target.agent_key,
                    }
                    for target in targets
                ]
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    application.add_api_route(
        "/api/onboarding/pairing-context",
        pairing_context,
        methods=["GET"],
    )


def _owner(request: Request, authentication: AccessAuthenticator) -> Principal | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not token or token != token.strip() or any(character.isspace() for character in token):
        return None
    try:
        return authentication.authenticate_access(token)
    except (AuthenticationFailed, TypeError, ValueError):
        return None


__all__ = ["register_pairing_context_route"]
