"""Bounded strict-JSON Cloud HTTP adapter for device pairing."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.pairing import (
    ConnectorToken,
    DeviceAuthenticationChallenge,
    DeviceAuthenticationTokenRequest,
    DeviceBinding,
    DeviceChallengeProof,
    DeviceChallengeRequest,
    PairingOffer,
    PairingOfferRequest,
    PairingOfferStatus,
)
from hermes_connector.ports.pairing import DevicePairingCloudError

_MAX_RESPONSE_BYTES = 65_536
_OFFER_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PAIRING_CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9_-]{43}$")
_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_SCOPES = frozenset({"session.observe", "session.control.request"})
_CREATE_OFFER_ERRORS = {
    "PAIRING_INVALID_REQUEST": 400,
    "IDEMPOTENCY_CONFLICT": 409,
    "RATE_LIMITED": 429,
}
_GET_OFFER_ERRORS = {
    "UNAUTHORIZED": 401,
    "PAIRING_NOT_FOUND": 404,
    "PAIRING_EXPIRED": 410,
    "RATE_LIMITED": 429,
}
_PROVE_PAIRING_ERRORS = {
    "UNAUTHORIZED": 401,
    "PAIRING_NOT_FOUND": 404,
    "PAIRING_STATE_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "PAIRING_EXPIRED": 410,
    "CHALLENGE_EXPIRED": 410,
    "CHALLENGE_INVALID": 401,
    "CHALLENGE_REPLAYED": 409,
    "RATE_LIMITED": 429,
}
_CREATE_DEVICE_CHALLENGE_ERRORS = {
    "DEVICE_AUTH_UNAVAILABLE": 403,
    "IDEMPOTENCY_CONFLICT": 409,
    "RATE_LIMITED": 429,
}
_ISSUE_DEVICE_TOKEN_ERRORS = {
    "CHALLENGE_INVALID": 401,
    "DEVICE_AUTH_UNAVAILABLE": 403,
    "IDEMPOTENCY_CONFLICT": 409,
    "CHALLENGE_EXPIRED": 410,
    "CHALLENGE_REPLAYED": 409,
    "RATE_LIMITED": 429,
}


class UnsafePairingHttpResponse(ValueError):
    """Cloud returned a response outside the pairing contract."""


class DevicePairingHttpClient:
    """Call the tenant-neutral pairing/device-auth HTTP API."""

    __slots__ = (
        "_client",
        "_closed",
        "_max_response_bytes",
        "_now",
        "_started",
        "_stop",
    )

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        _validate_base_url(base_url)
        if not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise ValueError("pairing response bound is invalid")
        self._max_response_bytes = max_response_bytes
        self._now = now
        self._closed = False
        self._started = False
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            follow_redirects=False,
            trust_env=False,
            headers={"accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        await self._client.aclose()

    @property
    def name(self) -> str:
        return "device_pairing_http"

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("device pairing HTTP client is closed")
        self._started = True

    async def ready(self) -> bool:
        return self._started and not self._closed

    async def run(self) -> None:
        await self._stop.wait()

    async def drain(self) -> None:
        return None

    async def stop(self) -> None:
        await self.aclose()

    async def create_pairing_offer(
        self,
        request: PairingOfferRequest,
        *,
        idempotency_key: UUID,
    ) -> PairingOffer:
        _validate_offer_request(request)
        value = await self._request_json(
            "POST",
            "api/device-pairing/offers",
            expected_status=201,
            headers={"Idempotency-Key": str(_uuid_value(idempotency_key))},
            body={
                "connector_instance_id": str(request.connector_instance_id),
                "connector_version": request.connector_version,
                "display_name": request.display_name,
                "key_algorithm": request.key_algorithm,
                "platform_family": request.platform_family,
                "public_key": request.public_key,
            },
            allowed_errors=_CREATE_OFFER_ERRORS,
        )
        return _parse_offer(value)

    async def get_pairing_offer(
        self,
        pairing_offer_id: UUID,
        *,
        pairing_offer_secret: str,
    ) -> PairingOfferStatus:
        secret = _offer_secret(pairing_offer_secret)
        value = await self._request_json(
            "GET",
            f"api/device-pairing/offers/{_uuid_value(pairing_offer_id)}",
            expected_status=200,
            headers={"X-Hermes-Pairing-Offer": secret},
            body=None,
            allowed_errors=_GET_OFFER_ERRORS,
        )
        return _parse_status(value)

    async def prove_pairing_session(
        self,
        pairing_session_id: UUID,
        proof: DeviceChallengeProof,
        *,
        pairing_offer_secret: str,
        idempotency_key: UUID,
    ) -> ConnectorToken:
        body = _proof_body(proof)
        value = await self._request_json(
            "POST",
            (f"api/device-pairing/sessions/{_uuid_value(pairing_session_id)}/proof"),
            expected_status=200,
            headers={
                "Idempotency-Key": str(_uuid_value(idempotency_key)),
                "X-Hermes-Pairing-Offer": _offer_secret(pairing_offer_secret),
            },
            body=body,
            allowed_errors=_PROVE_PAIRING_ERRORS,
        )
        return _parse_token(value, received_at=self._now())

    async def create_device_challenge(
        self,
        request: DeviceChallengeRequest,
        *,
        idempotency_key: UUID,
    ) -> DeviceAuthenticationChallenge:
        value = await self._request_json(
            "POST",
            "api/device-auth/challenges",
            expected_status=201,
            headers={"Idempotency-Key": str(_uuid_value(idempotency_key))},
            body={
                "credential_id": str(_uuid_value(request.credential_id)),
                "device_id": str(_uuid_value(request.device_id)),
            },
            allowed_errors=_CREATE_DEVICE_CHALLENGE_ERRORS,
        )
        return _parse_challenge(value)

    async def issue_device_token(
        self,
        request: DeviceAuthenticationTokenRequest,
        *,
        idempotency_key: UUID,
    ) -> ConnectorToken:
        proof = DeviceChallengeProof(
            challenge_id=request.challenge_id,
            signing_payload=request.signing_payload,
            signature_algorithm=request.signature_algorithm,
            signature=request.signature,
        )
        body = _proof_body(proof)
        body.update(
            {
                "credential_id": str(_uuid_value(request.credential_id)),
                "device_id": str(_uuid_value(request.device_id)),
            }
        )
        value = await self._request_json(
            "POST",
            "api/device-auth/tokens",
            expected_status=200,
            headers={"Idempotency-Key": str(_uuid_value(idempotency_key))},
            body=body,
            allowed_errors=_ISSUE_DEVICE_TOKEN_ERRORS,
        )
        return _parse_token(value, received_at=self._now())

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        headers: dict[str, str],
        body: dict[str, object] | None,
        allowed_errors: Mapping[str, int],
    ) -> dict[str, object]:
        content = None
        request_headers = dict(headers)
        if body is not None:
            content = json.dumps(
                body,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            request_headers["content-type"] = "application/json"
        try:
            async with self._client.stream(
                method,
                path,
                headers=request_headers,
                content=content,
            ) as response:
                raw = await self._read_bounded(response)
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise UnsafePairingHttpResponse(
                        "pairing response content type is invalid"
                    )
                value = _strict_json_object(raw)
                if response.status_code != expected_status:
                    _raise_cloud_error(
                        response.status_code,
                        value,
                        allowed_errors=allowed_errors,
                    )
                return value
        except httpx.TransportError:
            raise DevicePairingCloudError(
                code="PAIRING_TRANSPORT_UNAVAILABLE",
                status_code=503,
            ) from None

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                announced = int(content_length)
            except ValueError:
                raise UnsafePairingHttpResponse(
                    "pairing response length is invalid"
                ) from None
            if announced < 0 or announced > self._max_response_bytes:
                raise UnsafePairingHttpResponse("pairing response exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise UnsafePairingHttpResponse("pairing response exceeds size limit")
            chunks.append(chunk)
        if total == 0:
            raise UnsafePairingHttpResponse("pairing response body is empty")
        return b"".join(chunks)

    def __repr__(self) -> str:
        return "DevicePairingHttpClient(<cloud-endpoint>)"


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
        )
    except (UnicodeDecodeError, ValueError):
        raise UnsafePairingHttpResponse("pairing response JSON is invalid") from None
    if not isinstance(value, dict):
        raise UnsafePairingHttpResponse("pairing response JSON is invalid")
    return value


def _parse_offer(value: dict[str, object]) -> PairingOffer:
    _exact_fields(
        value,
        {
            "pairing_offer_id",
            "pairing_code",
            "pairing_offer_secret",
            "credential_fingerprint",
            "state",
            "revision",
            "ttl_seconds",
            "expires_at",
        },
    )
    if value["state"] != "pending" or value["ttl_seconds"] != 300:
        raise UnsafePairingHttpResponse("pairing offer response is invalid")
    revision = _positive_int(value["revision"])
    code = _pattern_string(value["pairing_code"], _PAIRING_CODE)
    fingerprint = _pattern_string(value["credential_fingerprint"], _FINGERPRINT)
    return PairingOffer(
        pairing_offer_id=_uuid(value["pairing_offer_id"]),
        pairing_code=code,
        pairing_offer_secret=_offer_secret(value["pairing_offer_secret"]),
        credential_fingerprint=fingerprint,
        state="pending",
        revision=revision,
        ttl_seconds=300,
        expires_at=_datetime(value["expires_at"]),
    )


def _parse_status(value: dict[str, object]) -> PairingOfferStatus:
    state = value.get("state")
    if state == "pending":
        _exact_fields(
            value,
            {
                "pairing_offer_id",
                "state",
                "activation_state",
                "expires_at",
                "revision",
            },
        )
        if value["activation_state"] != "waiting_owner":
            raise UnsafePairingHttpResponse("pairing status response is invalid")
        return PairingOfferStatus(
            pairing_offer_id=_uuid(value["pairing_offer_id"]),
            pairing_session_id=None,
            state="pending",
            activation_state="waiting_owner",
            binding=None,
            challenge=None,
            expires_at=_datetime(value["expires_at"]),
            revision=_positive_int(value["revision"]),
        )
    if state == "claimed":
        _exact_fields(
            value,
            {
                "pairing_offer_id",
                "pairing_session_id",
                "state",
                "activation_state",
                "expires_at",
                "revision",
            },
        )
        if value["activation_state"] != "waiting_owner_confirmation":
            raise UnsafePairingHttpResponse("pairing status response is invalid")
        return PairingOfferStatus(
            pairing_offer_id=_uuid(value["pairing_offer_id"]),
            pairing_session_id=_uuid(value["pairing_session_id"]),
            state="claimed",
            activation_state="waiting_owner_confirmation",
            binding=None,
            challenge=None,
            expires_at=_datetime(value["expires_at"]),
            revision=_positive_int(value["revision"]),
        )
    if state == "confirmed":
        activation = value.get("activation_state")
        common = {
            "pairing_offer_id",
            "pairing_session_id",
            "state",
            "activation_state",
            "binding",
            "expires_at",
            "revision",
        }
        if activation == "awaiting_proof":
            _exact_fields(value, common | {"challenge"})
            challenge = _parse_challenge_object(value["challenge"])
        elif activation == "active":
            _exact_fields(value, common)
            challenge = None
        else:
            raise UnsafePairingHttpResponse("pairing status response is invalid")
        return PairingOfferStatus(
            pairing_offer_id=_uuid(value["pairing_offer_id"]),
            pairing_session_id=_uuid(value["pairing_session_id"]),
            state="confirmed",
            activation_state=str(activation),
            binding=_parse_binding_object(value["binding"]),
            challenge=challenge,
            expires_at=_datetime(value["expires_at"]),
            revision=_positive_int(value["revision"]),
        )
    if state in {"expired", "cancelled"}:
        _exact_fields(
            value,
            {
                "pairing_offer_id",
                "state",
                "activation_state",
                "expires_at",
                "revision",
            },
        )
        if value["activation_state"] != "blocked":
            raise UnsafePairingHttpResponse("pairing status response is invalid")
        return PairingOfferStatus(
            pairing_offer_id=_uuid(value["pairing_offer_id"]),
            pairing_session_id=None,
            state=str(state),
            activation_state="blocked",
            binding=None,
            challenge=None,
            expires_at=_datetime(value["expires_at"]),
            revision=_positive_int(value["revision"]),
        )
    raise UnsafePairingHttpResponse("pairing status response is invalid")


def _proof_body(proof: DeviceChallengeProof) -> dict[str, object]:
    if (
        proof.signature_algorithm != "Ed25519"
        or not isinstance(proof.signing_payload, str)
        or not 64 <= len(proof.signing_payload) <= 1_024
        or _BASE64URL.fullmatch(proof.signing_payload) is None
        or not isinstance(proof.signature, str)
        or _SIGNATURE.fullmatch(proof.signature) is None
    ):
        raise ValueError("device challenge proof is invalid")
    return {
        "challenge_id": str(_uuid_value(proof.challenge_id)),
        "signature": proof.signature,
        "signature_algorithm": "Ed25519",
        "signing_payload": proof.signing_payload,
    }


def _parse_challenge(value: dict[str, object]) -> DeviceAuthenticationChallenge:
    return _parse_challenge_object(value)


def _parse_challenge_object(value: object) -> DeviceAuthenticationChallenge:
    if not isinstance(value, dict):
        raise UnsafePairingHttpResponse("device challenge response is invalid")
    _exact_fields(
        value,
        {"challenge_id", "signing_payload", "ttl_seconds", "expires_at"},
    )
    payload = value["signing_payload"]
    ttl = value["ttl_seconds"]
    if (
        not isinstance(payload, str)
        or not 64 <= len(payload) <= 1_024
        or _BASE64URL.fullmatch(payload) is None
        or type(ttl) is not int
        or not 1 <= ttl <= 60
    ):
        raise UnsafePairingHttpResponse("device challenge response is invalid")
    return DeviceAuthenticationChallenge(
        challenge_id=_uuid(value["challenge_id"]),
        signing_payload=payload,
        ttl_seconds=ttl,
        expires_at=_datetime(value["expires_at"]),
    )


def _parse_token(
    value: dict[str, object],
    *,
    received_at: datetime,
) -> ConnectorToken:
    _exact_fields(
        value,
        {"access_token", "token_type", "ttl_seconds", "expires_at", "binding"},
    )
    token = value["access_token"]
    ttl = value["ttl_seconds"]
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 4_096
        or token != token.strip()
        or any(character.isspace() for character in token)
        or value["token_type"] != "Bearer"
        or type(ttl) is not int
        or not 1 <= ttl <= 3_600
    ):
        raise UnsafePairingHttpResponse("device token response is invalid")
    expires_at = _datetime(value["expires_at"])
    if (
        expires_at <= received_at
        or expires_at > received_at + timedelta(seconds=ttl)
        or expires_at > received_at + timedelta(seconds=3_600)
    ):
        raise UnsafePairingHttpResponse("device token response is invalid")
    return ConnectorToken(
        access_token=token,
        token_type="Bearer",
        ttl_seconds=ttl,
        expires_at=expires_at,
        binding=_parse_binding_object(value["binding"]),
    )


def _parse_binding_object(value: object) -> DeviceBinding:
    if not isinstance(value, dict):
        raise UnsafePairingHttpResponse("device binding response is invalid")
    _exact_fields(
        value,
        {"tenant_id", "device_id", "credential_id", "agent_id", "scopes"},
    )
    scopes = value["scopes"]
    if (
        not isinstance(scopes, list)
        or not 1 <= len(scopes) <= 2
        or any(not isinstance(scope, str) or scope not in _SCOPES for scope in scopes)
        or len(set(scopes)) != len(scopes)
    ):
        raise UnsafePairingHttpResponse("device binding response is invalid")
    return DeviceBinding(
        tenant_id=_uuid(value["tenant_id"]),
        device_id=_uuid(value["device_id"]),
        credential_id=_uuid(value["credential_id"]),
        agent_id=_uuid(value["agent_id"]),
        scopes=tuple(scopes),
    )


def _raise_cloud_error(
    status_code: int,
    value: dict[str, object],
    *,
    allowed_errors: Mapping[str, int],
) -> None:
    _exact_fields(value, {"code", "reason"})
    code = value["code"]
    reason = value["reason"]
    if (
        not isinstance(code, str)
        or not 1 <= len(code) <= 128
        or not isinstance(reason, str)
        or not 1 <= len(reason) <= 256
    ):
        raise UnsafePairingHttpResponse("pairing error response is invalid")
    if allowed_errors.get(code) != status_code:
        raise UnsafePairingHttpResponse("pairing error response is invalid")
    raise DevicePairingCloudError(code=code, status_code=status_code)


def _validate_base_url(value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("pairing Cloud endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("pairing Cloud endpoint must use HTTPS")


def _validate_offer_request(request: PairingOfferRequest) -> None:
    _uuid_value(request.connector_instance_id)
    if (
        not isinstance(request.display_name, str)
        or not 1 <= len(request.display_name) <= 128
        or request.display_name != request.display_name.strip()
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", request.platform_family)
        or not 1 <= len(request.connector_version) <= 64
        or request.key_algorithm != "Ed25519"
        or not _PUBLIC_KEY.fullmatch(request.public_key)
    ):
        raise ValueError("pairing offer request is invalid")


def _uuid_value(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError("pairing UUID is invalid")
    return canonical_uuid(value)


def _uuid(value: object) -> UUID:
    try:
        return canonical_uuid(value)
    except (TypeError, ValueError):
        raise UnsafePairingHttpResponse("pairing UUID is invalid") from None


def _datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise UnsafePairingHttpResponse("pairing timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise UnsafePairingHttpResponse("pairing timestamp is invalid") from None
    if parsed.tzinfo != UTC:
        raise UnsafePairingHttpResponse("pairing timestamp is invalid")
    return parsed


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise UnsafePairingHttpResponse("pairing integer is invalid")
    return value


def _pattern_string(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise UnsafePairingHttpResponse("pairing response string is invalid")
    return value


def _offer_secret(value: object) -> str:
    if not isinstance(value, str) or _OFFER_SECRET.fullmatch(value) is None:
        raise ValueError("pairing offer credential is invalid")
    return value


def _exact_fields(value: dict[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise UnsafePairingHttpResponse("pairing response fields are invalid")
