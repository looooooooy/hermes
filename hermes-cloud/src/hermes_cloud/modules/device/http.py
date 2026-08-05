"""FastAPI transport adapter for the frozen device-pairing v1 surface."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from hermes_cloud.modules.cloud_api.application.service import AuthenticationFailed
from hermes_cloud.modules.cloud_api.domain import (
    Principal,
    is_canonical_rfc4122_uuid_v1_to_v5,
)
from hermes_cloud.modules.device.application import DeviceProofRejected
from hermes_cloud.modules.device.ports import (
    DeviceAuthenticationUnavailable,
    PairingChallengeExpired,
    PairingChallengeReplayed,
    PairingClaimRateLimited,
    PairingClaimUnavailable,
    PairingExpired,
    PairingIdempotencyConflict,
    PairingNotFound,
    PairingOfferAuthenticationFailed,
    PairingScopeUnavailable,
    PairingStateConflict,
)

_BASE64URL_32 = re.compile(r"^[A-Za-z0-9_-]{43}$")
_BASE64URL_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_BASE64URL_PAYLOAD = re.compile(r"^[A-Za-z0-9_-]{64,1024}$")
_PAIRING_CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9_-]{43}$")
_PLATFORM = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_MAX_JSON_BODY_BYTES = 16_384


class PairingHttpService(Protocol):
    def create_offer(self, **arguments: Any) -> Mapping[str, object]: ...

    def get_offer(self, **arguments: Any) -> Mapping[str, object]: ...

    def claim_offer(self, **arguments: Any) -> Mapping[str, object]: ...

    def get_pairing_session(self, **arguments: Any) -> Mapping[str, object]: ...

    def confirm_pairing(self, **arguments: Any) -> Mapping[str, object]: ...

    def cancel_pairing(self, **arguments: Any) -> Mapping[str, object]: ...

    def prove_pairing(self, **arguments: Any) -> Mapping[str, object]: ...

    def create_device_challenge(self, **arguments: Any) -> Mapping[str, object]: ...

    def mint_connector_token(self, **arguments: Any) -> Mapping[str, object]: ...

    def revoke_device(self, **arguments: Any) -> Mapping[str, object]: ...


class AccessAuthenticator(Protocol):
    def authenticate_access(self, token: str) -> Principal: ...


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _canonical_uuid(value: str) -> str:
    if not is_canonical_rfc4122_uuid_v1_to_v5(value):
        raise ValueError("value must be a canonical RFC 4122 UUID")
    return value


class CreatePairingOfferBody(_StrictBody):
    connector_instance_id: str
    display_name: str = Field(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")
    platform_family: str = Field(min_length=1, max_length=32)
    connector_version: str = Field(min_length=1, max_length=64)
    key_algorithm: Literal["Ed25519"]
    public_key: str

    _instance_uuid = field_validator("connector_instance_id")(_canonical_uuid)

    @field_validator("platform_family")
    @classmethod
    def _valid_platform(cls, value: str) -> str:
        if _PLATFORM.fullmatch(value) is None:
            raise ValueError("platform family is invalid")
        return value

    @field_validator("connector_version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if _VERSION.fullmatch(value) is None:
            raise ValueError("connector version is invalid")
        return value

    @field_validator("public_key")
    @classmethod
    def _valid_public_key(cls, value: str) -> str:
        if _BASE64URL_32.fullmatch(value) is None:
            raise ValueError("public key is invalid")
        return value


class ClaimPairingBody(_StrictBody):
    pairing_code: str
    workspace_id: str
    agent_id: str
    device_display_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^\S(?:.*\S)?$",
    )
    scopes: list[Literal["session.observe", "session.control.request"]] = Field(
        min_length=1,
        max_length=2,
    )
    expected_revision: int = Field(ge=1)

    _workspace_uuid = field_validator("workspace_id")(_canonical_uuid)
    _agent_uuid = field_validator("agent_id")(_canonical_uuid)

    @field_validator("pairing_code")
    @classmethod
    def _valid_pairing_code(cls, value: str) -> str:
        if _PAIRING_CODE.fullmatch(value) is None:
            raise ValueError("pairing code is invalid")
        return value

    @field_validator("scopes")
    @classmethod
    def _unique_scopes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("scopes must be unique")
        return value


class ConfirmPairingBody(_StrictBody):
    credential_fingerprint: str
    expected_revision: int = Field(ge=1)

    @field_validator("credential_fingerprint")
    @classmethod
    def _valid_fingerprint(cls, value: str) -> str:
        if _FINGERPRINT.fullmatch(value) is None:
            raise ValueError("credential fingerprint is invalid")
        return value


class CancelPairingBody(_StrictBody):
    reason: Literal["owner_cancelled", "fingerprint_mismatch"]
    expected_revision: int = Field(ge=1)


class DeviceChallengeProofBody(_StrictBody):
    challenge_id: str
    signing_payload: str
    signature_algorithm: Literal["Ed25519"]
    signature: str

    _challenge_uuid = field_validator("challenge_id")(_canonical_uuid)

    @field_validator("signing_payload")
    @classmethod
    def _valid_payload(cls, value: str) -> str:
        if _BASE64URL_PAYLOAD.fullmatch(value) is None:
            raise ValueError("signing payload is invalid")
        return value

    @field_validator("signature")
    @classmethod
    def _valid_signature(cls, value: str) -> str:
        if _BASE64URL_SIGNATURE.fullmatch(value) is None:
            raise ValueError("signature is invalid")
        return value


class DeviceAuthenticationChallengeBody(_StrictBody):
    device_id: str
    credential_id: str

    _device_uuid = field_validator("device_id")(_canonical_uuid)
    _credential_uuid = field_validator("credential_id")(_canonical_uuid)


class DeviceAuthenticationTokenBody(DeviceChallengeProofBody):
    device_id: str
    credential_id: str

    _device_uuid = field_validator("device_id")(_canonical_uuid)
    _credential_uuid = field_validator("credential_id")(_canonical_uuid)


class RevokeDeviceBody(_StrictBody):
    reason: Literal["user_requested", "device_lost", "security_event"]
    expected_revision: int = Field(ge=1)


def register_device_pairing_routes(
    application: FastAPI,
    *,
    authentication: AccessAuthenticator,
    pairing: PairingHttpService,
) -> None:
    async def create_offer(request: Request) -> JSONResponse:
        body = await _body(request, CreatePairingOfferBody)
        idempotency_key = _idempotency_key(request)
        if body is None or idempotency_key is None:
            return _invalid_request()
        return await _invoke(
            pairing.create_offer,
            status_code=201,
            request=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    async def get_offer(
        pairing_offer_id: str,
        request: Request,
    ) -> JSONResponse:
        offer_id = _uuid(pairing_offer_id)
        offer_secret = request.headers.get("X-Hermes-Pairing-Offer")
        if offer_id is None:
            return _invalid_request()
        if offer_secret is None or _BASE64URL_32.fullmatch(offer_secret) is None:
            return _unauthorized()
        return await _invoke(
            pairing.get_offer,
            pairing_offer_id=offer_id,
            pairing_offer_secret=offer_secret,
        )

    async def claim_offer(request: Request) -> JSONResponse:
        principal = _owner(request, authentication)
        if principal is None:
            return _unauthorized()
        body = await _body(request, ClaimPairingBody)
        idempotency_key = _idempotency_key(request)
        if body is None or idempotency_key is None:
            return _invalid_request()
        return await _invoke(
            pairing.claim_offer,
            principal=principal,
            request=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    async def confirm_pairing(
        pairing_session_id: str,
        request: Request,
    ) -> JSONResponse:
        return await _owner_session_mutation(
            pairing.confirm_pairing,
            pairing_session_id,
            request,
            authentication,
            ConfirmPairingBody,
        )

    async def get_pairing_session(
        pairing_session_id: str,
        request: Request,
    ) -> JSONResponse:
        principal = _owner(request, authentication)
        if principal is None:
            return _unauthorized()
        session_id = _uuid(pairing_session_id)
        if session_id is None:
            return _invalid_request()
        return await _invoke_owner_pairing_read(
            pairing.get_pairing_session,
            principal=principal,
            pairing_session_id=session_id,
        )

    async def cancel_pairing(
        pairing_session_id: str,
        request: Request,
    ) -> JSONResponse:
        return await _owner_session_mutation(
            pairing.cancel_pairing,
            pairing_session_id,
            request,
            authentication,
            CancelPairingBody,
        )

    async def prove_pairing(
        pairing_session_id: str,
        request: Request,
    ) -> JSONResponse:
        session_id = _uuid(pairing_session_id)
        offer_secret = request.headers.get("X-Hermes-Pairing-Offer")
        body = await _body(request, DeviceChallengeProofBody)
        idempotency_key = _idempotency_key(request)
        if (
            session_id is None
            or offer_secret is None
            or _BASE64URL_32.fullmatch(offer_secret) is None
        ):
            return _unauthorized()
        if body is None or idempotency_key is None:
            return _invalid_request()
        return await _invoke(
            pairing.prove_pairing,
            pairing_session_id=session_id,
            pairing_offer_secret=offer_secret,
            request=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    async def create_device_challenge(request: Request) -> JSONResponse:
        body = await _body(request, DeviceAuthenticationChallengeBody)
        idempotency_key = _idempotency_key(request)
        if body is None or idempotency_key is None:
            return _invalid_request()
        return await _invoke(
            pairing.create_device_challenge,
            status_code=201,
            request=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    async def mint_connector_token(request: Request) -> JSONResponse:
        body = await _body(request, DeviceAuthenticationTokenBody)
        idempotency_key = _idempotency_key(request)
        if body is None or idempotency_key is None:
            return _invalid_request()
        return await _invoke(
            pairing.mint_connector_token,
            request=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    async def revoke_device(
        device_id: str,
        request: Request,
    ) -> JSONResponse:
        principal = _owner(request, authentication)
        if principal is None:
            return _unauthorized()
        parsed_device_id = _uuid(device_id)
        body = await _body(request, RevokeDeviceBody)
        idempotency_key = _idempotency_key(request)
        if parsed_device_id is None or body is None or idempotency_key is None:
            return _invalid_request()
        return await _invoke(
            pairing.revoke_device,
            principal=principal,
            device_id=parsed_device_id,
            request=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    routes = (
        ("/api/device-pairing/offers", create_offer, ("POST",)),
        (
            "/api/device-pairing/offers/{pairing_offer_id}",
            get_offer,
            ("GET",),
        ),
        ("/api/device-pairing/claims", claim_offer, ("POST",)),
        (
            "/api/device-pairing/sessions/{pairing_session_id}",
            get_pairing_session,
            ("GET",),
        ),
        (
            "/api/device-pairing/sessions/{pairing_session_id}/confirm",
            confirm_pairing,
            ("POST",),
        ),
        (
            "/api/device-pairing/sessions/{pairing_session_id}/cancel",
            cancel_pairing,
            ("POST",),
        ),
        (
            "/api/device-pairing/sessions/{pairing_session_id}/proof",
            prove_pairing,
            ("POST",),
        ),
        ("/api/device-auth/challenges", create_device_challenge, ("POST",)),
        ("/api/device-auth/tokens", mint_connector_token, ("POST",)),
        ("/api/devices/{device_id}/revoke", revoke_device, ("POST",)),
    )
    for path, endpoint, methods in routes:
        application.add_api_route(path, endpoint, methods=list(methods))


async def _owner_session_mutation(
    operation: Any,
    pairing_session_id: str,
    request: Request,
    authentication: AccessAuthenticator,
    model: type[_StrictBody],
) -> JSONResponse:
    principal = _owner(request, authentication)
    if principal is None:
        return _unauthorized()
    session_id = _uuid(pairing_session_id)
    body = await _body(request, model)
    idempotency_key = _idempotency_key(request)
    if session_id is None or body is None or idempotency_key is None:
        return _invalid_request()
    return await _invoke(
        operation,
        principal=principal,
        pairing_session_id=session_id,
        request=body.model_dump(),
        idempotency_key=idempotency_key,
    )


async def _invoke(
    operation: Any,
    *,
    status_code: int = 200,
    **arguments: object,
) -> JSONResponse:
    try:
        result = await run_in_threadpool(operation, **arguments)
    except PairingClaimRateLimited as error:
        return JSONResponse(
            {
                "code": "PAIRING_CLAIM_RATE_LIMITED",
                "reason": "pairing claims temporarily unavailable",
            },
            status_code=429,
            headers={"Retry-After": str(error.retry_after_seconds)},
        )
    except PairingClaimUnavailable:
        return _error(
            404,
            "PAIRING_CLAIM_UNAVAILABLE",
            "pairing claim unavailable",
        )
    except PairingNotFound:
        return _error(404, "PAIRING_NOT_FOUND", "pairing resource not found")
    except PairingOfferAuthenticationFailed:
        return _error(401, "UNAUTHORIZED", "authentication required")
    except PairingScopeUnavailable:
        return _error(403, "FORBIDDEN", "operation is forbidden")
    except DeviceAuthenticationUnavailable:
        return _error(
            403,
            "DEVICE_AUTH_UNAVAILABLE",
            "device authentication unavailable",
        )
    except PairingExpired:
        return _error(410, "PAIRING_EXPIRED", "pairing is expired")
    except PairingIdempotencyConflict:
        return _error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "idempotency key conflicts",
        )
    except PairingChallengeExpired:
        return _error(410, "CHALLENGE_EXPIRED", "challenge is expired")
    except PairingChallengeReplayed:
        return _error(409, "CHALLENGE_REPLAYED", "challenge was already used")
    except DeviceProofRejected:
        return _error(401, "CHALLENGE_INVALID", "device proof is invalid")
    except PairingStateConflict:
        return _error(
            409,
            "PAIRING_STATE_CONFLICT",
            "pairing state conflicts",
        )
    except (TypeError, ValueError):
        return _invalid_request()
    return JSONResponse(dict(result), status_code=status_code)


async def _invoke_owner_pairing_read(
    operation: Any,
    **arguments: object,
) -> JSONResponse:
    try:
        result = await run_in_threadpool(operation, **arguments)
    except (PairingNotFound, PairingScopeUnavailable):
        return _error(404, "PAIRING_NOT_FOUND", "pairing resource not found")
    return JSONResponse(
        dict(result),
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


async def _body(request: Request, model: type[_StrictBody]) -> _StrictBody | None:
    try:
        content_type = request.headers.get("content-type", "")
        parts = [part.strip().lower() for part in content_type.split(";")]
        if (
            not parts
            or parts[0] != "application/json"
            or len(parts) > 2
            or (len(parts) == 2 and parts[1] != "charset=utf-8")
        ):
            return None
        content_length = request.headers.get("content-length")
        if content_length is not None:
            parsed_length = int(content_length)
            if parsed_length < 0 or parsed_length > _MAX_JSON_BODY_BYTES:
                return None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_JSON_BODY_BYTES:
                return None
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
        return model.model_validate(payload)
    except (UnicodeDecodeError, TypeError, ValueError, ValidationError):
        return None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _owner(
    request: Request,
    authentication: AccessAuthenticator,
) -> Principal | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if (
        not token
        or token != token.strip()
        or any(character.isspace() for character in token)
    ):
        return None
    try:
        return authentication.authenticate_access(token)
    except (AuthenticationFailed, TypeError, ValueError):
        return None


def _idempotency_key(request: Request) -> UUID | None:
    return _uuid(request.headers.get("Idempotency-Key", ""))


def _uuid(value: str) -> UUID | None:
    if not is_canonical_rfc4122_uuid_v1_to_v5(value):
        return None
    return UUID(value)


def _invalid_request() -> JSONResponse:
    return JSONResponse(
        {
            "code": "PAIRING_INVALID_REQUEST",
            "reason": "pairing request is invalid",
        },
        status_code=400,
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {
            "code": "UNAUTHORIZED",
            "reason": "authentication required",
        },
        status_code=401,
    )


def _error(status_code: int, code: str, reason: str) -> JSONResponse:
    return JSONResponse(
        {
            "code": code,
            "reason": reason,
        },
        status_code=status_code,
    )
