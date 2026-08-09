"""Browser-assisted native OAuth/PKCE flow for Hermes Desktop and mobile clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.application.service import AuthenticationFailed
from hermes_cloud.modules.cloud_api.domain import IssuedAuthentication

_CODE_TTL_SECONDS = 300
_MAX_FORM_BYTES = 16 * 1024
_PKCE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_STATE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_ALLOWED_AUTHORIZE_QUERY = frozenset(
    {
        "code_challenge",
        "code_challenge_method",
        "redirect_uri",
        "state",
        "provider",
        "request_id",
    }
)
_REQUIRED_AUTHORIZE_FIELDS = frozenset(
    {"code_challenge", "code_challenge_method", "redirect_uri", "state", "provider"}
)
_ALLOWED_FORM_FIELDS = _ALLOWED_AUTHORIZE_QUERY | frozenset({"username", "password"})
_REQUIRED_FORM_FIELDS = _REQUIRED_AUTHORIZE_FIELDS | frozenset({"username", "password"})


class NativeAuthIssuer(Protocol):
    def issue_password_login(
        self,
        *,
        provider: str,
        subject: str,
        password: str,
        next_path: str,
    ) -> IssuedAuthentication: ...


@dataclass(frozen=True, slots=True)
class _AuthorizationRequest:
    code_challenge: str
    redirect_uri: str
    state: str
    provider: str
    request_id: str | None


@dataclass(frozen=True, slots=True)
class _AuthorizationCode:
    challenge: str
    issued: IssuedAuthentication
    expires_at: float


@dataclass(frozen=True, slots=True)
class _AuthorizationPoll:
    challenge: str
    state: str
    code: str
    expires_at: float


class _AuthorizationCodeStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._codes: dict[str, _AuthorizationCode] = {}

    def issue(self, challenge: str, issued: IssuedAuthentication) -> str:
        now = time.monotonic()
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._prune(now)
            self._codes[code] = _AuthorizationCode(
                challenge=challenge,
                issued=issued,
                expires_at=now + _CODE_TTL_SECONDS,
            )
        return code

    def consume(self, code: str, verifier: str) -> IssuedAuthentication | None:
        if _CODE.fullmatch(code) is None or _PKCE_VERIFIER.fullmatch(verifier) is None:
            return None
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._codes.pop(code, None)
        if record is None or record.expires_at <= now:
            return None
        expected = _pkce_challenge(verifier)
        if not hmac.compare_digest(expected, record.challenge):
            return None
        return record.issued

    def _prune(self, now: float) -> None:
        expired = [code for code, record in self._codes.items() if record.expires_at <= now]
        for code in expired:
            self._codes.pop(code, None)


class _AuthorizationPollStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _AuthorizationPoll] = {}

    def complete(self, request_id: str, *, challenge: str, state: str, code: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._records[request_id] = _AuthorizationPoll(
                challenge=challenge,
                state=state,
                code=code,
                expires_at=now + _CODE_TTL_SECONDS,
            )

    def consume(self, request_id: str, verifier: str, state: str) -> str | None:
        if (
            _REQUEST_ID.fullmatch(request_id) is None
            or _PKCE_VERIFIER.fullmatch(verifier) is None
            or _STATE.fullmatch(state) is None
        ):
            return None
        expected = _pkce_challenge(verifier)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.get(request_id)
            if record is None or record.expires_at <= now:
                return None
            if not hmac.compare_digest(expected, record.challenge) or not hmac.compare_digest(
                state, record.state
            ):
                return None
            self._records.pop(request_id, None)
            return record.code

    def _prune(self, now: float) -> None:
        expired = [
            request_id
            for request_id, record in self._records.items()
            if record.expires_at <= now
        ]
        for request_id in expired:
            self._records.pop(request_id, None)


def register_native_auth_routes(
    application: FastAPI,
    *,
    authentication: NativeAuthIssuer,
) -> None:
    codes = _AuthorizationCodeStore()
    polls = _AuthorizationPollStore()

    async def authorize_get(request: Request):
        parsed = _parse_authorization_request(dict(request.query_params))
        if parsed is None:
            return _invalid_request()
        return _login_page(parsed)

    async def authorize_post(request: Request):
        form = await _read_form(request)
        if (
            form is None
            or not _REQUIRED_FORM_FIELDS <= set(form)
            or not set(form) <= _ALLOWED_FORM_FIELDS
        ):
            return _invalid_request()
        oauth_fields = {key: form[key] for key in _REQUIRED_AUTHORIZE_FIELDS}
        if "request_id" in form:
            oauth_fields["request_id"] = form["request_id"]
        parsed = _parse_authorization_request(oauth_fields)
        if parsed is None:
            return _invalid_request()
        username = form["username"]
        password = form["password"]
        if not 1 <= len(username) <= 254 or not 1 <= len(password) <= 1024:
            return _login_page(parsed, failed=True, status_code=401)
        try:
            issued = await run_in_threadpool(
                authentication.issue_password_login,
                provider=parsed.provider,
                subject=username,
                password=password,
                next_path="",
            )
        except (AuthenticationFailed, TypeError, ValueError):
            return _login_page(parsed, failed=True, status_code=401)
        code = codes.issue(parsed.code_challenge, issued)
        if parsed.request_id is not None:
            polls.complete(
                parsed.request_id,
                challenge=parsed.code_challenge,
                state=parsed.state,
                code=code,
            )
            return _login_complete_page()
        return RedirectResponse(
            _callback_url(parsed.redirect_uri, code=code, state=parsed.state),
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def poll(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _poll_error()
        if not isinstance(body, dict) or set(body) != {
            "request_id",
            "code_verifier",
            "state",
        }:
            return _poll_error()
        request_id = body.get("request_id")
        verifier = body.get("code_verifier")
        state = body.get("state")
        if not all(isinstance(value, str) for value in (request_id, verifier, state)):
            return _poll_error()
        assert isinstance(request_id, str)
        assert isinstance(verifier, str)
        assert isinstance(state, str)
        if (
            _REQUEST_ID.fullmatch(request_id) is None
            or _PKCE_VERIFIER.fullmatch(verifier) is None
            or _STATE.fullmatch(state) is None
        ):
            return _poll_error()
        code = polls.consume(request_id, verifier, state)
        if code is None:
            return JSONResponse(
                {"status": "pending"},
                status_code=202,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        return JSONResponse(
            {"code": code},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def exchange(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _token_error()
        if not isinstance(body, dict) or set(body) != {"code", "code_verifier"}:
            return _token_error()
        code = body.get("code")
        verifier = body.get("code_verifier")
        if not isinstance(code, str) or not isinstance(verifier, str):
            return _token_error()
        issued = codes.consume(code, verifier)
        if issued is None:
            return _token_error()
        return JSONResponse(
            {
                "access_token": issued.access_token.reveal(),
                "refresh_token": issued.refresh_token.reveal(),
                "token_type": "Bearer",
                "expires_at": int(issued.access_expires_at.timestamp()),
                "provider": "basic",
                "user_id": str(issued.user_id),
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    application.add_api_route(
        "/auth/native/authorize",
        authorize_get,
        methods=["GET"],
        include_in_schema=False,
        response_model=None,
    )
    application.add_api_route(
        "/auth/native/authorize",
        authorize_post,
        methods=["POST"],
        include_in_schema=False,
        response_model=None,
    )
    application.add_api_route(
        "/auth/native/poll",
        poll,
        methods=["POST"],
        include_in_schema=False,
        response_model=None,
    )
    application.add_api_route(
        "/auth/native/token",
        exchange,
        methods=["POST"],
        include_in_schema=False,
        response_model=None,
    )


def _parse_authorization_request(values: dict[str, str]) -> _AuthorizationRequest | None:
    if not set(values) <= _ALLOWED_AUTHORIZE_QUERY:
        return None
    normalized = dict(values)
    normalized.setdefault("provider", "basic")
    request_id = normalized.pop("request_id", None)
    if set(normalized) != _REQUIRED_AUTHORIZE_FIELDS:
        return None
    challenge = normalized["code_challenge"]
    method = normalized["code_challenge_method"]
    redirect_uri = normalized["redirect_uri"]
    state = normalized["state"]
    provider = normalized["provider"]
    if (
        _PKCE_CHALLENGE.fullmatch(challenge) is None
        or method != "S256"
        or _STATE.fullmatch(state) is None
        or provider != "basic"
        or not _valid_loopback_redirect(redirect_uri)
        or (request_id is not None and _REQUEST_ID.fullmatch(request_id) is None)
    ):
        return None
    return _AuthorizationRequest(challenge, redirect_uri, state, provider, request_id)


def _valid_loopback_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and port is not None
        and 1 <= port <= 65535
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/oauth/callback"
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _callback_url(redirect_uri: str, *, code: str, state: str) -> str:
    parsed = urlsplit(redirect_uri)
    query = urlencode({"code": code, "state": state})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


async def _read_form(request: Request) -> dict[str, str] | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            if not 0 <= int(raw_length) <= _MAX_FORM_BYTES:
                return None
        except ValueError:
            return None
    body = await request.body()
    if not body or len(body) > _MAX_FORM_BYTES:
        return None
    try:
        decoded = body.decode("utf-8")
        parsed = parse_qs(
            decoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=16,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if any(len(items) != 1 for items in parsed.values()):
        return None
    return {key: items[0] for key, items in parsed.items()}


def _login_page(
    request: _AuthorizationRequest,
    *,
    failed: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    fields = [
        ("code_challenge", request.code_challenge),
        ("code_challenge_method", "S256"),
        ("redirect_uri", request.redirect_uri),
        ("state", request.state),
        ("provider", request.provider),
    ]
    if request.request_id is not None:
        fields.append(("request_id", request.request_id))
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value, quote=True)}">'
        for key, value in fields
    )
    error = '<p class="error">Sign-in failed. Check the account and try again.</p>' if failed else ""
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to Hermes</title>
<style>body{{font:15px system-ui;background:#081210;color:#d8e7e2;display:grid;place-items:center;min-height:100vh;margin:0}}main{{width:min(420px,calc(100% - 40px));padding:30px;border:1px solid #1e3831;border-radius:18px;background:#0b1715}}h1{{font-size:24px;margin:0 0 8px}}p{{color:#8da39d;line-height:1.5}}label{{display:block;margin:16px 0 6px;color:#a8bcb6}}input{{box-sizing:border-box;width:100%;padding:12px;border-radius:10px;border:1px solid #29483f;background:#07110f;color:#eef7f4}}button{{width:100%;margin-top:20px;padding:12px;border:0;border-radius:10px;background:#6fe3bd;color:#07110f;font-weight:700}}.error{{color:#ffaaa2}}</style>
</head><body><main><h1>Connect Hermes</h1><p>Sign in to approve this computer. Hermes Desktop will finish the secure PKCE exchange automatically.</p>{error}
<form method="post" autocomplete="on">{hidden}<label for="username">Account</label><input id="username" name="username" autocomplete="username" required maxlength="254"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1024"><button type="submit">Sign in and continue</button></form></main></body></html>"""
    return HTMLResponse(document, status_code=status_code, headers=_browser_headers())


def _login_complete_page() -> HTMLResponse:
    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes sign-in complete</title>
<style>body{font:15px system-ui;background:#081210;color:#d8e7e2;display:grid;place-items:center;min-height:100vh;margin:0}main{width:min(420px,calc(100% - 40px));padding:30px;border:1px solid #1e3831;border-radius:18px;background:#0b1715}h1{font-size:24px;margin:0 0 8px}p{color:#8da39d;line-height:1.5}</style>
</head><body><main><h1>Hermes sign-in complete</h1><p>You can return to Hermes Desktop. This browser page does not need to connect to localhost.</p></main></body></html>"""
    return HTMLResponse(document, headers=_browser_headers())


def _browser_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _invalid_request() -> JSONResponse:
    return JSONResponse(
        {"code": "INVALID_NATIVE_AUTH_REQUEST", "reason": "invalid native authorization request"},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def _poll_error() -> JSONResponse:
    return JSONResponse(
        {"code": "INVALID_NATIVE_AUTH_POLL", "reason": "invalid native authorization poll"},
        status_code=400,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _token_error() -> JSONResponse:
    return JSONResponse(
        {"code": "AUTHENTICATION_FAILED", "reason": "authorization code is invalid or expired"},
        status_code=401,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
