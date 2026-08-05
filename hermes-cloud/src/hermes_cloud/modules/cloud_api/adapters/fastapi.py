"""FastAPI adapter for the external Cloud P0 compatibility surface."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from hermes_cloud.application.asgi_health import HealthApplication
from hermes_cloud.modules.cloud_api.adapters.http_auth import (
    forbidden_response,
    is_trusted_same_origin_mutation,
)
from hermes_cloud.modules.cloud_api.adapters.http_sessions import (
    register_session_routes,
)
from hermes_cloud.modules.cloud_api.adapters.http_tickets import (
    register_ticket_route,
)
from hermes_cloud.modules.cloud_api.adapters.realtime import (
    register_realtime_route,
)
from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
    LogoutFailed,
)
from hermes_cloud.modules.cloud_api.application.sessions import SessionQueryService
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings
from hermes_cloud.modules.cloud_api.ports import (
    LoginTenantResolverPort,
    ObserverSubscriptionPort,
    ProjectionEventSourcePort,
    SecretResolverPort,
)
from hermes_cloud.modules.control.ports import ControlRuntimePort
from hermes_cloud.modules.device.http import (
    PairingHttpService,
    register_device_pairing_routes,
)
from hermes_cloud.modules.identity.ports import IdentityRepositoryPort
from hermes_cloud.modules.projection.ports import (
    ObserverProjectionRepositoryPort,
    SessionCatalogRepositoryPort,
    SessionProjectionRepositoryPort,
)
from hermes_cloud.ports.dependency_probe import DependencyProbe


class BusinessApiApplication(FastAPI):
    """FastAPI application retaining the established component lifecycle API."""

    def __init__(
        self,
        dependency_probes: Iterable[DependencyProbe] = (),
        *,
        service: CloudApiService | None = None,
        session_queries: SessionQueryService | None = None,
        projection_event_source: ProjectionEventSourcePort | None = None,
        observer_subscription_manager: ObserverSubscriptionPort | None = None,
        control_runtime: ControlRuntimePort | None = None,
        pairing_service: PairingHttpService | None = None,
    ) -> None:
        self._health_application = HealthApplication(
            "business-api",
            dependency_probes,
        )
        self._cloud_api_service = service

        super().__init__(
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        self.add_api_route("/live", self._live, methods=["GET"])
        self.add_api_route("/ready", self._ready, methods=["GET"])
        self.add_api_route("/api/status", self._status, methods=["GET"])
        self.add_api_route(
            "/auth/password-login",
            self._password_login,
            methods=["POST"],
        )
        self.add_api_route(
            "/auth/native/refresh",
            self._refresh,
            methods=["POST"],
        )
        self.add_api_route(
            "/auth/logout",
            self._logout,
            methods=["POST"],
        )
        if service is not None and session_queries is not None:
            register_session_routes(
                self,
                authentication=service,
                sessions=session_queries,
            )
        if service is not None:
            if pairing_service is not None:
                register_device_pairing_routes(
                    self,
                    authentication=service,
                    pairing=pairing_service,
                )
            register_ticket_route(
                self,
                service=service,
                sessions=session_queries,
            )
            register_realtime_route(
                self,
                authentication=service,
                sessions=session_queries,
                event_source=projection_event_source,
                subscription_manager=observer_subscription_manager,
                control_runtime=control_runtime,
            )
        self.add_exception_handler(404, self._not_found)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] == "lifespan":
            await self._health_application(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def startup(self) -> None:
        await self._health_application.startup()

    async def shutdown(self) -> None:
        await self._health_application.shutdown()

    def snapshot(self) -> dict[str, object]:
        return self._health_application.snapshot()

    async def _live(self) -> JSONResponse:
        snapshot = self.snapshot()
        return JSONResponse(snapshot, status_code=200 if snapshot["live"] else 503)

    async def _ready(self) -> JSONResponse:
        snapshot = self.snapshot()
        return JSONResponse(snapshot, status_code=200 if snapshot["ready"] else 503)

    async def _status(self) -> dict[str, object]:
        snapshot = self.snapshot()
        if self._cloud_api_service is not None and snapshot["ready"]:
            return {
                "gateway_running": True,
                "gateway_state": "ready",
                "auth_required": True,
                "auth_providers": ["basic"],
                "auth_flows": ["password"],
                "overall": "healthy",
            }
        return {
            "gateway_running": True,
            "gateway_state": "degraded",
            "auth_required": False,
            "auth_providers": [],
            "auth_flows": [],
            "overall": "degraded",
        }

    async def _password_login(self, request: Request) -> JSONResponse:
        if self._cloud_api_service is None:
            return _authentication_error()
        try:
            body = await request.json()
        except ValueError:
            return _authentication_error()
        if not isinstance(body, dict) or set(body) != {
            "provider",
            "username",
            "password",
            "next",
        }:
            return _authentication_error()
        try:
            issued = await run_in_threadpool(
                self._cloud_api_service.issue_password_login,
                provider=body.get("provider"),
                subject=body.get("username"),
                password=body.get("password"),
                next_path=body.get("next"),
            )
        except (AuthenticationFailed, TypeError, ValueError):
            return _authentication_error()
        response = JSONResponse({"ok": True})
        cookie_common = {
            "secure": True,
            "httponly": True,
            "samesite": "strict",
            "path": "/",
        }
        response.set_cookie(
            "hermes_session_at",
            issued.access_token.reveal(),
            max_age=int(self._cloud_api_service.access_ttl_seconds),
            expires=issued.access_expires_at,
            **cookie_common,
        )
        response.set_cookie(
            "hermes_session_rt",
            issued.refresh_token.reveal(),
            **cookie_common,
        )
        response.set_cookie(
            "hermes_session_provider",
            "basic",
            **cookie_common,
        )
        return response

    async def _refresh(self, request: Request) -> JSONResponse:
        if self._cloud_api_service is None:
            return _authentication_error()
        try:
            body = await request.json()
        except ValueError:
            return _authentication_error()
        if not isinstance(body, dict) or set(body) != {
            "refresh_token",
            "provider",
        }:
            return _authentication_error()
        try:
            issued = await run_in_threadpool(
                self._cloud_api_service.rotate_refresh,
                provider=body.get("provider"),
                refresh_token=body.get("refresh_token"),
            )
        except (AuthenticationFailed, TypeError, ValueError):
            return _authentication_error()
        return JSONResponse(
            {
                "access_token": issued.access_token.reveal(),
                "refresh_token": issued.refresh_token.reveal(),
                "token_type": "Bearer",
                "expires_at": int(issued.access_expires_at.timestamp()),
                "provider": "basic",
                "user_id": str(issued.user_id),
            }
        )

    async def _logout(self, request: Request) -> JSONResponse:
        service = self._cloud_api_service
        if service is None:
            return _authentication_error()
        if not is_trusted_same_origin_mutation(request, service):
            return forbidden_response()
        if not await _has_empty_body(request):
            return JSONResponse(
                {
                    "code": "INVALID_REQUEST",
                    "reason": "empty request body required",
                },
                status_code=400,
            )
        try:
            await run_in_threadpool(
                service.logout_browser_session,
                access_token=request.cookies.get("hermes_session_at") or None,
                refresh_token=request.cookies.get("hermes_session_rt") or None,
            )
        except AuthenticationFailed:
            return _authentication_error()
        except LogoutFailed:
            return JSONResponse(
                {"code": "LOGOUT_FAILED", "reason": "logout failed"},
                status_code=503,
            )
        response = JSONResponse({"ok": True})
        for name in (
            "hermes_session_at",
            "hermes_session_rt",
            "hermes_session_provider",
        ):
            response.delete_cookie(
                name,
                path="/",
                secure=True,
                httponly=True,
                samesite="strict",
            )
        return response

    @staticmethod
    async def _not_found(
        _request: object,
        _error: HTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "category": "PROTOCOL",
                "code": "ROUTE_NOT_FOUND",
                "retryable": False,
            },
            status_code=404,
        )


def build_fastapi_application(
    dependency_probes: Iterable[DependencyProbe] = (),
    *,
    identity_repository: IdentityRepositoryPort | None = None,
    projection_repository: SessionProjectionRepositoryPort | None = None,
    session_catalog_repository: SessionCatalogRepositoryPort | None = None,
    observer_projection_repository: ObserverProjectionRepositoryPort | None = None,
    projection_event_source: ProjectionEventSourcePort | None = None,
    observer_subscription_manager: ObserverSubscriptionPort | None = None,
    control_runtime: ControlRuntimePort | None = None,
    tenant_resolver: LoginTenantResolverPort | None = None,
    secret_resolver: SecretResolverPort | None = None,
    settings: Mapping[str, object] | CloudApiSettings | None = None,
    now: Callable[[], datetime] | None = None,
    pairing_service: PairingHttpService | None = None,
) -> BusinessApiApplication:
    service = None
    if (
        identity_repository is not None
        and tenant_resolver is not None
        and secret_resolver is not None
        and settings is not None
    ):
        parsed_settings = (
            settings
            if isinstance(settings, CloudApiSettings)
            else CloudApiSettings.from_mapping(settings)
        )
        service = CloudApiService(
            identity_repository=identity_repository,
            tenant_resolver=tenant_resolver,
            secret_resolver=secret_resolver,
            settings=parsed_settings,
            now=now,
        )
    session_queries = (
        SessionQueryService(
            projection_repository,
            observer_repository=observer_projection_repository,
            catalog_repository=session_catalog_repository,
        )
        if projection_repository is not None
        else None
    )
    return BusinessApiApplication(
        dependency_probes,
        service=service,
        session_queries=session_queries,
        projection_event_source=projection_event_source,
        observer_subscription_manager=observer_subscription_manager,
        control_runtime=control_runtime,
        pairing_service=pairing_service,
    )


def _authentication_error() -> JSONResponse:
    return JSONResponse(
        {
            "code": "AUTHENTICATION_FAILED",
            "reason": "authentication failed",
        },
        status_code=401,
    )


async def _has_empty_body(request: Request) -> bool:
    content_lengths = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == b"content-length"
    ]
    if len(content_lengths) > 1 or (
        content_lengths and content_lengths[0] != "0"
    ):
        return False
    if request.headers.get("transfer-encoding") is not None:
        return False
    async for chunk in request.stream():
        if chunk:
            return False
    return True
