"""Shared HTTP bearer authentication for Cloud API adapters."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
)
from hermes_cloud.modules.cloud_api.domain import Principal

_ACCESS_COOKIE = "hermes_session_at"


def is_trusted_same_origin_mutation(
    request: Request,
    service: CloudApiService,
) -> bool:
    return _is_trusted_same_origin(
        request,
        trusted_forwarded_proxy_hosts=service.trusted_forwarded_proxy_hosts,
    )


async def authenticate_bearer(
    request: Request,
    service: CloudApiService,
) -> Principal | JSONResponse:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme != "Bearer" or not token:
        return unauthorized_response()
    try:
        return await run_in_threadpool(service.authenticate_access, token)
    except AuthenticationFailed:
        return unauthorized_response()


async def authenticate_ticket_request(
    request: Request,
    service: CloudApiService,
) -> Principal | JSONResponse:
    """Authenticate native Bearer or browser cookie ticket mutations.

    Bearer remains the compatibility authority. When both credentials are
    present, both must be valid and name the exact same principal. Cookie-only
    authentication additionally requires an HTTPS same-origin request.
    """

    authorization = request.headers.get("authorization")
    cookie_token = request.cookies.get(_ACCESS_COOKIE)
    if authorization is not None:
        principal = await authenticate_bearer(request, service)
        if isinstance(principal, JSONResponse):
            return principal
        if cookie_token is None:
            return principal
        cookie_principal = await _authenticate_access(cookie_token, service)
        if cookie_principal is None or cookie_principal != principal:
            return unauthorized_response()
        return principal
    if cookie_token is None:
        return unauthorized_response()
    if not _is_trusted_same_origin(
        request,
        trusted_forwarded_proxy_hosts=service.trusted_forwarded_proxy_hosts,
    ):
        return forbidden_response()
    principal = await _authenticate_access(cookie_token, service)
    return principal if principal is not None else unauthorized_response()


async def authenticate_catalog_request(
    request: Request,
    service: CloudApiService,
) -> Principal | JSONResponse:
    """Authenticate native Bearer or an HTTPS same-origin browser read."""

    authorization = request.headers.get("authorization")
    cookie_token = request.cookies.get(_ACCESS_COOKIE)
    if authorization is not None:
        principal = await authenticate_bearer(request, service)
        if isinstance(principal, JSONResponse):
            return principal
        if cookie_token is None:
            return principal
        cookie_principal = await _authenticate_access(cookie_token, service)
        if cookie_principal is None or cookie_principal != principal:
            return unauthorized_response()
        return principal
    if cookie_token is None:
        return unauthorized_response()
    if not _is_trusted_browser_read(
        request,
        trusted_forwarded_proxy_hosts=service.trusted_forwarded_proxy_hosts,
    ):
        return forbidden_response()
    principal = await _authenticate_access(cookie_token, service)
    return principal if principal is not None else unauthorized_response()


async def _authenticate_access(
    token: str,
    service: CloudApiService,
) -> Principal | None:
    try:
        return await run_in_threadpool(service.authenticate_access, token)
    except AuthenticationFailed:
        return None


def _is_trusted_same_origin(
    request: Request,
    *,
    trusted_forwarded_proxy_hosts: tuple[str, ...] = (),
) -> bool:
    origin = request.headers.get("origin")
    if (
        origin is None
        or not _is_effective_https(
            request,
            trusted_forwarded_proxy_hosts=trusted_forwarded_proxy_hosts,
        )
    ):
        return False
    try:
        parsed = urlsplit(origin)
        request_host = request.url.hostname
        request_port = request.url.port or 443
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.hostname is not None
        and request_host is not None
        and parsed.hostname.casefold() == request_host.casefold()
        and (parsed.port or 443) == request_port
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _is_trusted_browser_read(
    request: Request,
    *,
    trusted_forwarded_proxy_hosts: tuple[str, ...],
) -> bool:
    if not _is_effective_https(
        request,
        trusted_forwarded_proxy_hosts=trusted_forwarded_proxy_hosts,
    ):
        return False
    if request.headers.get("origin") is not None:
        return _is_trusted_same_origin(
            request,
            trusted_forwarded_proxy_hosts=trusted_forwarded_proxy_hosts,
        )
    return (
        request.headers.get("sec-fetch-site") == "same-origin"
        and request.headers.get("sec-fetch-mode") == "cors"
        and request.headers.get("sec-fetch-dest") == "empty"
    )


def _is_effective_https(
    request: Request,
    *,
    trusted_forwarded_proxy_hosts: tuple[str, ...],
) -> bool:
    if request.url.scheme == "https":
        return True
    client = request.client
    return (
        request.url.scheme == "http"
        and client is not None
        and client.host in trusted_forwarded_proxy_hosts
        and _single_header(request, "x-forwarded-proto") == "https"
    )


def _single_header(request: Request, name: str) -> str | None:
    encoded_name = name.encode("ascii")
    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == encoded_name
    ]
    return values[0] if len(values) == 1 else None


def unauthorized_response() -> JSONResponse:
    return JSONResponse(
        {
            "code": "UNAUTHORIZED",
            "reason": "authorization required",
        },
        status_code=401,
    )


def forbidden_response() -> JSONResponse:
    return JSONResponse(
        {
            "code": "FORBIDDEN",
            "reason": "trusted same-origin request required",
        },
        status_code=403,
    )
