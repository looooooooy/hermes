from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from hermes_cloud.modules.cloud_api.adapters.fastapi import BusinessApiApplication

IDEMPOTENCY_KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OFFER_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
DEVICE_ID = "77777777-7777-4777-8777-777777777777"


class _Authentication:
    def authenticate_access(self, token: str) -> object:
        assert token == "owner-access-token"
        return type(
            "Principal",
            (),
            {
                "tenant_id": UUID("33333333-3333-4333-8333-333333333333"),
                "user_id": UUID("44444444-4444-4444-8444-444444444444"),
            },
        )()


class _PairingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, operation: str, **arguments: Any) -> dict[str, object]:
        self.calls.append((operation, arguments))
        return {"operation": operation}

    def create_offer(self, **arguments: Any) -> dict[str, object]:
        return self._record("create_offer", **arguments)

    def get_offer(self, **arguments: Any) -> dict[str, object]:
        return self._record("get_offer", **arguments)

    def claim_offer(self, **arguments: Any) -> dict[str, object]:
        return self._record("claim_offer", **arguments)

    def get_pairing_session(self, **arguments: Any) -> dict[str, object]:
        response = self._record("get_pairing_session", **arguments)
        return {
            **response,
            "revision": 2,
            "device_revision": 4,
        }

    def confirm_pairing(self, **arguments: Any) -> dict[str, object]:
        return self._record("confirm_pairing", **arguments)

    def cancel_pairing(self, **arguments: Any) -> dict[str, object]:
        return self._record("cancel_pairing", **arguments)

    def prove_pairing(self, **arguments: Any) -> dict[str, object]:
        return self._record("prove_pairing", **arguments)

    def create_device_challenge(self, **arguments: Any) -> dict[str, object]:
        return self._record("create_device_challenge", **arguments)

    def mint_connector_token(self, **arguments: Any) -> dict[str, object]:
        return self._record("mint_connector_token", **arguments)

    def revoke_device(self, **arguments: Any) -> dict[str, object]:
        return self._record("revoke_device", **arguments)


def _app(service: _PairingService) -> BusinessApiApplication:
    return BusinessApiApplication(
        service=_Authentication(),  # type: ignore[arg-type]
        pairing_service=service,  # type: ignore[arg-type]
    )


def test_business_api_registers_exact_device_pairing_routes() -> None:
    service = _PairingService()
    application = _app(service)

    actual = {
        (method, route.path)
        for route in application.routes
        for method in getattr(route, "methods", ())
        if route.path.startswith(
            ("/api/device-pairing", "/api/device-auth", "/api/devices")
        )
    }
    assert actual == {
        ("POST", "/api/device-pairing/offers"),
        ("GET", "/api/device-pairing/offers/{pairing_offer_id}"),
        ("POST", "/api/device-pairing/claims"),
        ("GET", "/api/device-pairing/sessions/{pairing_session_id}"),
        ("POST", "/api/device-pairing/sessions/{pairing_session_id}/confirm"),
        ("POST", "/api/device-pairing/sessions/{pairing_session_id}/cancel"),
        ("POST", "/api/device-pairing/sessions/{pairing_session_id}/proof"),
        ("POST", "/api/device-auth/challenges"),
        ("POST", "/api/device-auth/tokens"),
        ("POST", "/api/devices/{device_id}/revoke"),
    }


def test_create_offer_is_strict_and_passes_canonical_uuid_header() -> None:
    service = _PairingService()
    valid = {
        "connector_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "display_name": "Hermes Connector",
        "platform_family": "macos",
        "connector_version": "1.0.0",
        "key_algorithm": "Ed25519",
        "public_key": "A" * 43,
    }

    with TestClient(_app(service)) as client:
        response = client.post(
            "/api/device-pairing/offers",
            headers={"Idempotency-Key": IDEMPOTENCY_KEY},
            json=valid,
        )
        extra = client.post(
            "/api/device-pairing/offers",
            headers={"Idempotency-Key": IDEMPOTENCY_KEY},
            json={**valid, "tenant_id": "forbidden"},
        )
        invalid_key = client.post(
            "/api/device-pairing/offers",
            headers={"Idempotency-Key": "not-a-uuid"},
            json=valid,
        )

    assert response.status_code == 201
    assert service.calls[0][0] == "create_offer"
    assert service.calls[0][1]["idempotency_key"] == UUID(IDEMPOTENCY_KEY)
    assert extra.status_code == 400
    assert invalid_key.status_code == 400


def test_pairing_json_rejects_wrong_media_type_duplicate_keys_and_oversize() -> None:
    service = _PairingService()
    valid_json = (
        '{"connector_instance_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
        '"display_name":"Hermes Connector","platform_family":"macos",'
        '"connector_version":"1.0.0","key_algorithm":"Ed25519",'
        f'"public_key":"{"A" * 43}"}}'
    )
    nested_duplicate = (
        '{"connector_instance_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
        '"display_name":"Hermes Connector","platform_family":"macos",'
        '"connector_version":"1.0.0","key_algorithm":"Ed25519",'
        f'"public_key":"{"A" * 43}",'
        '"extra":{"value":1,"value":2}}'
    )

    with TestClient(_app(service)) as client:
        wrong_media_type = client.post(
            "/api/device-pairing/offers",
            headers={
                "Idempotency-Key": IDEMPOTENCY_KEY,
                "Content-Type": "text/plain",
            },
            content=valid_json,
        )
        duplicate = client.post(
            "/api/device-pairing/offers",
            headers={
                "Idempotency-Key": IDEMPOTENCY_KEY,
                "Content-Type": "application/json",
            },
            content=nested_duplicate,
        )
        oversized = client.post(
            "/api/device-pairing/offers",
            headers={
                "Idempotency-Key": IDEMPOTENCY_KEY,
                "Content-Type": "application/json",
            },
            content="{" + '"padding":"' + ("x" * 70_000) + '"}',
        )

    assert wrong_media_type.status_code == 400
    assert duplicate.status_code == 400
    assert oversized.status_code == 400
    assert service.calls == []


def test_owner_and_offer_auth_are_taken_only_from_required_headers() -> None:
    service = _PairingService()
    claim = {
        "pairing_code": "2AB3-C4D5",
        "workspace_id": "55555555-5555-4555-8555-555555555555",
        "agent_id": "66666666-6666-4666-8666-666666666666",
        "device_display_name": "Office Mac",
        "scopes": ["session.observe"],
        "expected_revision": 1,
    }

    with TestClient(_app(service)) as client:
        unauthenticated = client.post(
            "/api/device-pairing/claims",
            headers={"Idempotency-Key": IDEMPOTENCY_KEY},
            json=claim,
        )
        claimed = client.post(
            "/api/device-pairing/claims",
            headers={
                "Authorization": "Bearer owner-access-token",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            json=claim,
        )
        missing_offer_secret = client.get(
            f"/api/device-pairing/offers/{OFFER_ID}",
        )
        status = client.get(
            f"/api/device-pairing/offers/{OFFER_ID}",
            headers={"X-Hermes-Pairing-Offer": "s" * 43},
        )

    assert unauthenticated.status_code == 401
    assert claimed.status_code == 200
    assert service.calls[0][1]["principal"].tenant_id == UUID(
        "33333333-3333-4333-8333-333333333333"
    )
    assert missing_offer_secret.status_code == 401
    assert status.status_code == 200
    assert service.calls[1][1]["pairing_offer_secret"] == "s" * 43


def test_owner_pairing_status_is_read_only_uncached_and_owner_scoped() -> None:
    service = _PairingService()

    with TestClient(_app(service)) as client:
        unauthenticated = client.get(
            f"/api/device-pairing/sessions/{SESSION_ID}",
        )
        response = client.get(
            f"/api/device-pairing/sessions/{SESSION_ID}",
            headers={
                "Authorization": "Bearer owner-access-token",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "operation": "get_pairing_session",
        "revision": 2,
        "device_revision": 4,
    }
    assert len(service.calls) == 1
    operation, arguments = service.calls[0]
    assert operation == "get_pairing_session"
    assert arguments["principal"].tenant_id == UUID(
        "33333333-3333-4333-8333-333333333333"
    )
    assert arguments["pairing_session_id"] == UUID(SESSION_ID)
    assert "idempotency_key" not in arguments
