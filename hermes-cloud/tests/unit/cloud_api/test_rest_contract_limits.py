from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_cloud.modules.cloud_api.adapters.http_sessions import _bounded_response
from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
)
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings


def _nested_object(depth: int) -> dict[str, object]:
    value: dict[str, object] = {}
    for _ in range(depth - 1):
        value = {"nested": value}
    return value


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "界" * 43_691},
        {("k" * 131_073): None},
        _nested_object(33),
        {f"field-{index}": None for index in range(1025)},
        {"items": [None] * 1025},
        {"number": float("nan")},
        {"number": float("inf")},
    ],
)
def test_rest_response_rejects_recursive_contract_limit_violations(
    payload: dict[str, object],
) -> None:
    response = _bounded_response(payload, 4 * 1024 * 1024)

    assert response.status_code == 413
    assert response.body == (
        b'{"code":"RESPONSE_TOO_LARGE","reason":"response exceeds contract limit"}'
    )


class _Resolver:
    def __init__(self) -> None:
        self.subjects: list[object] = []

    def tenant_for_subject(self, subject: str):
        self.subjects.append(subject)
        raise AssertionError("oversized input reached tenant resolver")


class _SecretResolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, reference: str) -> bytes:
        self.references.append(reference)
        raise AssertionError("oversized input reached secret resolver")


def _service(
    resolver: _Resolver,
    secret_resolver: _SecretResolver,
) -> CloudApiService:
    return CloudApiService(
        identity_repository=object(),  # type: ignore[arg-type]
        tenant_resolver=resolver,
        secret_resolver=secret_resolver,
        settings=CloudApiSettings(
            signing_secret_ref="secret-manager/unit/cloud-api",
            access_ttl_seconds=300,
            refresh_ttl_seconds=3600,
            ticket_ttl_seconds=60,
        ),
        now=lambda: datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("subject", "password"),
    [
        ("u" * 255, "password"),
        ("user@example.test", "p" * 1025),
        (["not", "a", "string"], "password"),
        ("user@example.test", ["not", "a", "string"]),
    ],
)
def test_login_rejects_invalid_or_oversized_fields_before_dependencies(
    subject: object,
    password: object,
) -> None:
    resolver = _Resolver()
    secret_resolver = _SecretResolver()

    with pytest.raises(AuthenticationFailed):
        _service(resolver, secret_resolver).issue_password_login(
            provider="basic",
            subject=subject,  # type: ignore[arg-type]
            password=password,  # type: ignore[arg-type]
            next_path="",
        )

    assert resolver.subjects == []
    assert secret_resolver.references == []


def test_refresh_rejects_oversized_token_before_secret_or_repository() -> None:
    resolver = _Resolver()
    secret_resolver = _SecretResolver()
    oversized_but_structured = f"{'a' * 2048}.{'b' * 2048}"

    with pytest.raises(AuthenticationFailed):
        _service(resolver, secret_resolver).rotate_refresh(
            provider="basic",
            refresh_token=oversized_but_structured,
        )

    assert secret_resolver.references == []


@pytest.mark.parametrize(
    "hosts",
    [
        ("203.0.113.9",),
        ("127.0.0.1", "127.0.0.1"),
        ("not-an-ip",),
        "127.0.0.1",
    ],
)
def test_forwarded_proxy_allowlist_accepts_only_unique_loopback_ip_sequences(
    hosts: object,
) -> None:
    values = {
        "signing_secret_ref": "secret-manager/unit/cloud-api",
        "access_ttl_seconds": 300,
        "refresh_ttl_seconds": 3600,
        "ticket_ttl_seconds": 60,
        "trusted_forwarded_proxy_hosts": hosts,
    }

    with pytest.raises(ValueError, match="trusted forwarded proxy"):
        CloudApiSettings.from_mapping(values)
