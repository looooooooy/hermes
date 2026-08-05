from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from hermes_connector.adapters.cloud.pairing_http import (
    DevicePairingHttpClient,
    UnsafePairingHttpResponse,
)
from hermes_connector.domain.pairing import (
    DeviceAuthenticationTokenRequest,
    DeviceChallengeProof,
    DeviceChallengeRequest,
    PairingOfferRequest,
)
from hermes_connector.ports.pairing import DevicePairingCloudError

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
OFFER_ID = UUID("22222222-2222-4222-8222-222222222222")
IDEMPOTENCY_KEY = UUID("33333333-3333-4333-8333-333333333333")


@pytest.mark.asyncio
async def test_create_offer_sends_exact_tenant_neutral_json_over_https() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            headers={"content-type": "application/json"},
            json={
                "pairing_offer_id": str(OFFER_ID),
                "pairing_code": "ABCD-EFGH",
                "pairing_offer_secret": "S" * 43,
                "credential_fingerprint": "SHA256:" + "B" * 43,
                "state": "pending",
                "revision": 1,
                "ttl_seconds": 300,
                "expires_at": "2026-07-31T12:05:00Z",
            },
        )

    client = DevicePairingHttpClient(
        "https://cloud.example.test/hermes",
        transport=httpx.MockTransport(handler),
    )
    offer = await client.create_pairing_offer(
        PairingOfferRequest(
            connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
            display_name="Office Mac",
            platform_family="macos",
            connector_version="1.2.3",
            key_algorithm="Ed25519",
            public_key="A" * 43,
        ),
        idempotency_key=IDEMPOTENCY_KEY,
    )
    await client.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://cloud.example.test/hermes/api/device-pairing/offers"
    )
    assert request.headers["Idempotency-Key"] == str(IDEMPOTENCY_KEY)
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "connector_instance_id": "11111111-1111-4111-8111-111111111111",
        "connector_version": "1.2.3",
        "display_name": "Office Mac",
        "key_algorithm": "Ed25519",
        "platform_family": "macos",
        "public_key": "A" * 43,
    }
    assert offer.pairing_offer_id == OFFER_ID
    assert offer.expires_at == datetime(2026, 7, 31, 12, 5, tzinfo=UTC)
    assert "ABCD-EFGH" not in repr(offer)
    assert "S" * 43 not in repr(offer)


@pytest.mark.asyncio
async def test_poll_places_offer_secret_only_in_required_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "pairing_offer_id": str(OFFER_ID),
                "state": "pending",
                "activation_state": "waiting_owner",
                "expires_at": "2026-07-31T12:05:00Z",
                "revision": 2,
            },
        )

    client = DevicePairingHttpClient(
        "https://cloud.example.test/hermes",
        transport=httpx.MockTransport(handler),
    )
    response = await client.get_pairing_offer(
        OFFER_ID,
        pairing_offer_secret="S" * 43,
    )
    await client.aclose()

    request = requests[0]
    assert request.method == "GET"
    assert request.url.query == b""
    assert request.headers["X-Hermes-Pairing-Offer"] == "S" * 43
    assert "authorization" not in request.headers
    assert response.state == "pending"
    assert "S" * 43 not in repr(client)


@pytest.mark.asyncio
async def test_strict_response_rejects_unknown_or_duplicate_json_fields() -> None:
    bodies = (
        (
            b'{"pairing_offer_id":"22222222-2222-4222-8222-222222222222",'
            b'"state":"pending","activation_state":"waiting_owner",'
            b'"expires_at":"2026-07-31T12:05:00Z","revision":2,"extra":true}'
        ),
        (
            b'{"pairing_offer_id":"22222222-2222-4222-8222-222222222222",'
            b'"state":"pending","state":"pending",'
            b'"activation_state":"waiting_owner",'
            b'"expires_at":"2026-07-31T12:05:00Z","revision":2}'
        ),
    )

    for body in bodies:
        client = DevicePairingHttpClient(
            "https://cloud.example.test/hermes",
            transport=httpx.MockTransport(
                lambda _request, body=body: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=body,
                )
            ),
        )
        with pytest.raises(UnsafePairingHttpResponse):
            await client.get_pairing_offer(
                OFFER_ID,
                pairing_offer_secret="S" * 43,
            )
        await client.aclose()


@pytest.mark.asyncio
async def test_get_offer_accepts_only_its_frozen_status_code_pairs() -> None:
    responses = (
        (410, "PAIRING_EXPIRED", DevicePairingCloudError),
        (403, "DEVICE_AUTH_UNAVAILABLE", UnsafePairingHttpResponse),
        (410, "RATE_LIMITED", UnsafePairingHttpResponse),
    )
    for status, code, expected_error in responses:
        client = DevicePairingHttpClient(
            "https://cloud.example.test/hermes",
            transport=httpx.MockTransport(
                lambda _request, status=status, code=code: httpx.Response(
                    status,
                    json={"code": code, "reason": "redacted"},
                )
            ),
            now=lambda: NOW,
        )

        with pytest.raises(expected_error):
            await client.get_pairing_offer(
                OFFER_ID,
                pairing_offer_secret="S" * 43,
            )
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("request", "truncated_response"))
async def test_all_httpx_transport_errors_map_to_safe_unavailable(
    failure_stage: str,
) -> None:
    class _TruncatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"pairing_offer_id":'
            raise httpx.RemoteProtocolError("truncated response")

    def handler(_request: httpx.Request) -> httpx.Response:
        if failure_stage == "request":
            raise httpx.RemoteProtocolError("invalid peer framing")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_TruncatedStream(),
        )

    client = DevicePairingHttpClient(
        "https://cloud.example.test/hermes",
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )

    with pytest.raises(DevicePairingCloudError) as raised:
        await client.get_pairing_offer(
            OFFER_ID,
            pairing_offer_secret="S" * 43,
        )
    await client.aclose()

    assert raised.value.code == "PAIRING_TRANSPORT_UNAVAILABLE"
    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_http_cancellation_propagates_without_transport_remapping() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    client = DevicePairingHttpClient(
        "https://cloud.example.test/hermes",
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )

    with pytest.raises(asyncio.CancelledError):
        await client.get_pairing_offer(
            OFFER_ID,
            pairing_offer_secret="S" * 43,
        )
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires_at",
    (
        "2026-07-31T11:59:59Z",
        "2026-07-31T12:05:00.000001Z",
        "2026-07-31T13:00:01Z",
    ),
)
async def test_token_expiry_must_be_future_and_within_receipt_ttl(
    expires_at: str,
) -> None:
    binding = {
        "tenant_id": "66666666-6666-4666-8666-666666666666",
        "device_id": "77777777-7777-4777-8777-777777777777",
        "credential_id": "88888888-8888-4888-8888-888888888888",
        "agent_id": "99999999-9999-4999-8999-999999999999",
        "scopes": ["session.observe"],
    }
    client = DevicePairingHttpClient(
        "https://cloud.example.test/hermes",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "access_token": "T" * 64,
                    "token_type": "Bearer",
                    "ttl_seconds": 300,
                    "expires_at": expires_at,
                    "binding": binding,
                },
            )
        ),
        now=lambda: NOW,
    )

    with pytest.raises(UnsafePairingHttpResponse, match="token response"):
        await client.issue_device_token(
            DeviceAuthenticationTokenRequest(
                device_id=UUID(binding["device_id"]),
                credential_id=UUID(binding["credential_id"]),
                challenge_id=UUID("55555555-5555-4555-8555-555555555555"),
                signing_payload="A" * 64,
                signature_algorithm="Ed25519",
                signature="C" * 86,
            ),
            idempotency_key=IDEMPOTENCY_KEY,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_proof_and_token_renewal_use_exact_contract_headers_and_bodies() -> None:
    session_id = UUID("44444444-4444-4444-8444-444444444444")
    challenge_id = UUID("55555555-5555-4555-8555-555555555555")
    device_id = UUID("77777777-7777-4777-8777-777777777777")
    credential_id = UUID("88888888-8888-4888-8888-888888888888")
    signing_payload = "A" * 64
    signature = "C" * 86
    requests: list[httpx.Request] = []
    binding = {
        "tenant_id": "66666666-6666-4666-8666-666666666666",
        "device_id": str(device_id),
        "credential_id": str(credential_id),
        "agent_id": "99999999-9999-4999-8999-999999999999",
        "scopes": ["session.observe"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            body = {
                "pairing_offer_id": str(OFFER_ID),
                "pairing_session_id": str(session_id),
                "state": "confirmed",
                "activation_state": "awaiting_proof",
                "binding": binding,
                "challenge": {
                    "challenge_id": str(challenge_id),
                    "signing_payload": signing_payload,
                    "ttl_seconds": 60,
                    "expires_at": "2026-07-31T12:01:00Z",
                },
                "expires_at": "2026-07-31T12:05:00Z",
                "revision": 3,
            }
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/proof"):
            return httpx.Response(
                200,
                json={
                    "access_token": "T" * 64,
                    "token_type": "Bearer",
                    "ttl_seconds": 300,
                    "expires_at": "2026-07-31T12:05:00Z",
                    "binding": binding,
                },
            )
        if request.url.path.endswith("/device-auth/challenges"):
            return httpx.Response(
                201,
                json={
                    "challenge_id": str(challenge_id),
                    "signing_payload": signing_payload,
                    "ttl_seconds": 60,
                    "expires_at": "2026-07-31T12:01:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "N" * 64,
                "token_type": "Bearer",
                "ttl_seconds": 300,
                "expires_at": "2026-07-31T12:05:00Z",
                "binding": binding,
            },
        )

    client = DevicePairingHttpClient(
        "https://cloud.example.test/hermes",
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    status = await client.get_pairing_offer(
        OFFER_ID,
        pairing_offer_secret="S" * 43,
    )
    proof_token = await client.prove_pairing_session(
        session_id,
        DeviceChallengeProof(
            challenge_id=challenge_id,
            signing_payload=signing_payload,
            signature_algorithm="Ed25519",
            signature=signature,
        ),
        pairing_offer_secret="S" * 43,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    challenge = await client.create_device_challenge(
        DeviceChallengeRequest(
            device_id=device_id,
            credential_id=credential_id,
        ),
        idempotency_key=IDEMPOTENCY_KEY,
    )
    renewed = await client.issue_device_token(
        DeviceAuthenticationTokenRequest(
            device_id=device_id,
            credential_id=credential_id,
            challenge_id=challenge_id,
            signing_payload=signing_payload,
            signature_algorithm="Ed25519",
            signature=signature,
        ),
        idempotency_key=IDEMPOTENCY_KEY,
    )
    await client.aclose()

    assert status.pairing_session_id == session_id
    assert status.binding is not None
    assert status.challenge == challenge
    assert proof_token.access_token == "T" * 64
    assert renewed.access_token == "N" * 64
    proof_request = requests[1]
    assert proof_request.headers["X-Hermes-Pairing-Offer"] == "S" * 43
    assert proof_request.headers["Idempotency-Key"] == str(IDEMPOTENCY_KEY)
    assert json.loads(proof_request.content) == {
        "challenge_id": str(challenge_id),
        "signature": signature,
        "signature_algorithm": "Ed25519",
        "signing_payload": signing_payload,
    }
    challenge_request = requests[2]
    assert "authorization" not in challenge_request.headers
    assert json.loads(challenge_request.content) == {
        "credential_id": str(credential_id),
        "device_id": str(device_id),
    }
    token_request = requests[3]
    assert "authorization" not in token_request.headers
    assert json.loads(token_request.content)["signature"] == signature
