from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from hermes_cloud.modules.cloud_api.adapters.fastapi import BusinessApiApplication
from hermes_cloud.modules.release_update.domain import (
    DeviceUpdateContextV1,
    DownloadGrantV1,
    UpdateDecisionStatusV1,
    UpdateDecisionV1,
)

TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")


class _Authentication:
    trusted_forwarded_proxy_hosts: tuple[str, ...] = ()

    def authenticate_access(self, token: str) -> object:
        if token != "owner-access-token":
            raise RuntimeError("unexpected test token")
        return type(
            "Principal",
            (),
            {
                "tenant_id": TENANT_ID,
                "user_id": UUID("44444444-4444-4444-8444-444444444444"),
            },
        )()


class _Updates:
    def __init__(self) -> None:
        self.calls: list[DeviceUpdateContextV1] = []

    def check(self, context: DeviceUpdateContextV1) -> UpdateDecisionV1:
        self.calls.append(context)
        expires_at = datetime(2026, 8, 7, 16, 10, tzinfo=UTC)
        return UpdateDecisionV1(
            status=UpdateDecisionStatusV1.AVAILABLE,
            reason_code="eligible_update",
            release_id="1.0.1+20260807.1.gabcdef12",
            product_version="1.0.1",
            release_generation=101,
            channel="stable",
            rollout_bucket=42,
            mandatory=False,
            release_envelope={"schema_version": 1, "signature": "release-signature"},
            channel_envelope={"schema_version": 1, "signature": "channel-signature"},
            block_envelope={"schema_version": 1, "signature": "block-signature"},
            download_grants=(
                DownloadGrantV1(
                    object_key="artifacts/v1/sha256/aa/" + "a" * 64 + "/payload.bin",
                    sha256="a" * 64,
                    size_bytes=4096,
                    url="https://updates.example.test/artifacts/payload.bin?grant=short-lived",
                    expires_at=expires_at,
                ),
            ),
        )


def _app(updates: _Updates) -> BusinessApiApplication:
    return BusinessApiApplication(
        service=_Authentication(),  # type: ignore[arg-type]
        update_check_service=updates,  # type: ignore[arg-type]
    )


def _valid_body() -> dict[str, object]:
    return {
        "device_id": "77777777-7777-4777-8777-777777777777",
        "target": "windows-x86_64",
        "os_version": "10.0.26100",
        "active_release_id": "1.0.0+20260801.1.g00000000",
        "active_release_generation": 100,
        "highest_release_generation": 100,
        "requested_channel": "stable",
        "enterprise_pin_release_id": None,
    }


def test_update_check_requires_bearer_and_derives_organization_from_principal() -> None:
    updates = _Updates()
    with TestClient(_app(updates)) as client:
        unauthorized = client.post("/api/desktop/update-check", json=_valid_body())
        response = client.post(
            "/api/desktop/update-check",
            headers={"Authorization": "Bearer owner-access-token"},
            json=_valid_body(),
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert len(updates.calls) == 1
    context = updates.calls[0]
    assert context.organization_id == str(TENANT_ID)
    assert context.device_id == "77777777-7777-4777-8777-777777777777"
    assert context.requested_channel == "stable"
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["status"] == "available"
    assert payload["release_id"] == "1.0.1+20260807.1.gabcdef12"
    assert payload["release_envelope"]["signature"] == "release-signature"
    assert payload["download_grants"] == [
        {
            "object_key": "artifacts/v1/sha256/aa/" + "a" * 64 + "/payload.bin",
            "sha256": "a" * 64,
            "size_bytes": 4096,
            "url": "https://updates.example.test/artifacts/payload.bin?grant=short-lived",
            "expires_at": "2026-08-07T16:10:00Z",
        }
    ]


def test_update_check_rejects_client_supplied_organization_and_duplicate_json_keys() -> None:
    updates = _Updates()
    supplied_org = {**_valid_body(), "organization_id": "attacker-org"}
    duplicate = (
        '{"device_id":"77777777-7777-4777-8777-777777777777",'
        '"device_id":"88888888-8888-4888-8888-888888888888",'
        '"target":"windows-x86_64","os_version":"10.0.26100",'
        '"active_release_id":"1.0.0+20260801.1.g00000000",'
        '"active_release_generation":100,"highest_release_generation":100,'
        '"requested_channel":"stable","enterprise_pin_release_id":null}'
    )

    with TestClient(_app(updates)) as client:
        extra = client.post(
            "/api/desktop/update-check",
            headers={"Authorization": "Bearer owner-access-token"},
            json=supplied_org,
        )
        duplicated = client.post(
            "/api/desktop/update-check",
            headers={
                "Authorization": "Bearer owner-access-token",
                "Content-Type": "application/json",
            },
            content=duplicate,
        )

    assert extra.status_code == 400
    assert duplicated.status_code == 400
    assert updates.calls == []


def test_update_check_rejects_wrong_media_type_oversize_and_invalid_generation() -> None:
    updates = _Updates()
    invalid_generation = {**_valid_body(), "highest_release_generation": True}

    with TestClient(_app(updates)) as client:
        wrong_type = client.post(
            "/api/desktop/update-check",
            headers={
                "Authorization": "Bearer owner-access-token",
                "Content-Type": "text/plain",
            },
            content="{}",
        )
        oversized = client.post(
            "/api/desktop/update-check",
            headers={
                "Authorization": "Bearer owner-access-token",
                "Content-Type": "application/json",
            },
            content='{"padding":"' + ("x" * (33 * 1024)) + '"}',
        )
        invalid = client.post(
            "/api/desktop/update-check",
            headers={"Authorization": "Bearer owner-access-token"},
            json=invalid_generation,
        )

    assert wrong_type.status_code == 400
    assert oversized.status_code == 400
    assert invalid.status_code == 400
    assert updates.calls == []
