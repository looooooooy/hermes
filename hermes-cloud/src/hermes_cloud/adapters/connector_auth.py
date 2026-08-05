"""Fail-closed HMAC JWT authentication for Connector Gateway deployments."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Protocol
from uuid import UUID

from hermes_cloud.configuration import ConfigurationError, DsnFileReference
from hermes_cloud.domain.connector_gateway import (
    ConnectorAuthenticationExpired,
    ConnectorAuthorizationRevoked,
    ConnectorAuthorizationSuspended,
    ConnectorAuthorizationUnavailable,
    ConnectorIdentity,
)
from hermes_cloud.modules.device.ports import (
    DeviceAuthorizationRevoked,
    DeviceAuthorizationSuspended,
)

CONNECTOR_TOKEN_SCOPE = "connector.connect"
MAX_CONNECTOR_TOKEN_TTL_SECONDS = 3_600
MIN_CONNECTOR_SIGNING_SECRET_BYTES = 32
MAX_CONNECTOR_SIGNING_SECRET_BYTES = 4_096

_CLAIMS = frozenset(
    {
        "tenant_id",
        "device_id",
        "scope",
        "iat",
        "nbf",
        "exp",
    }
)
_V1_CLAIMS = frozenset(
    {
        "tenant_id",
        "device_id",
        "credential_id",
        "agent_id",
        "scopes",
        "jti",
        "iat",
        "nbf",
        "exp",
    }
)
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ConnectorAuthenticationConfigurationError(ValueError):
    """Raised when Connector authentication cannot be composed safely."""


class ConnectorDeviceAuthority(Protocol):
    def active_legacy_device_binding(
        self,
        *,
        tenant_id: UUID,
        device_id: UUID,
        now: datetime,
    ) -> object: ...

    def active_device_binding(
        self,
        *,
        tenant_id: UUID | None,
        device_id: UUID,
        credential_id: UUID,
        now: datetime,
    ) -> object: ...


def _load_jwt_runtime() -> ModuleType:
    try:
        runtime = import_module("jwt")
    except Exception:  # noqa: BLE001 - readiness must fail closed on a broken runtime.
        raise ConnectorAuthenticationConfigurationError(
            "connector JWT runtime is unavailable"
        ) from None
    if not callable(getattr(runtime, "get_unverified_header", None)) or not callable(
        getattr(runtime, "decode", None)
    ):
        raise ConnectorAuthenticationConfigurationError(
            "connector JWT runtime is unavailable"
        )
    return runtime


def validate_connector_identity(value: object) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError("connector identity is invalid")
    return value


def read_connector_signing_secret(path: str) -> bytes:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConnectorAuthenticationConfigurationError(
            "connector signing credential path must be absolute"
        )
    try:
        secret = DsnFileReference(path).read().encode("utf-8")
    except ConfigurationError:
        raise ConnectorAuthenticationConfigurationError(
            "connector signing credential is invalid"
        ) from None
    if not (
        MIN_CONNECTOR_SIGNING_SECRET_BYTES
        <= len(secret)
        <= MAX_CONNECTOR_SIGNING_SECRET_BYTES
    ):
        raise ConnectorAuthenticationConfigurationError(
            "connector signing credential size is invalid"
        )
    return secret


class HmacJwtConnectorAuthenticator:
    """Authenticate exact short-lived Connector claims signed with HS256."""

    name = "connector-authentication"
    critical = True
    deadline_seconds = 0.5

    def __init__(
        self,
        signing_secret: bytes,
        *,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        device_authority: ConnectorDeviceAuthority | None = None,
    ) -> None:
        if not isinstance(signing_secret, bytes) or not (
            MIN_CONNECTOR_SIGNING_SECRET_BYTES
            <= len(signing_secret)
            <= MAX_CONNECTOR_SIGNING_SECRET_BYTES
        ):
            raise ConnectorAuthenticationConfigurationError(
                "connector signing credential size is invalid"
            )
        self._signing_secret = signing_secret
        self._utc_now = utc_now
        self._device_authority = device_authority
        self._jwt_runtime = _load_jwt_runtime()

    async def authenticate(self, bearer_token: str) -> ConnectorIdentity:
        try:
            if (
                not isinstance(bearer_token, str)
                or not 1 <= len(bearer_token) <= 4_096
                or bearer_token != bearer_token.strip()
                or any(character.isspace() for character in bearer_token)
            ):
                raise ValueError
            header = self._jwt_runtime.get_unverified_header(bearer_token)
            if set(header) != {"alg", "typ"}:
                raise ValueError
            if header["alg"] != "HS256" or header["typ"] != "JWT":
                raise ValueError
            claims = self._jwt_runtime.decode(
                bearer_token,
                self._signing_secret,
                algorithms=["HS256"],
                options={"require": ["exp", "iat", "nbf"]},
            )
            if not isinstance(claims, dict):
                raise TypeError
            iat = claims["iat"]
            nbf = claims["nbf"]
            expires = claims["exp"]
            if type(iat) is not int or type(nbf) is not int or type(expires) is not int:
                raise ValueError
            now = self._now_seconds()
            if not (
                iat <= nbf <= now < expires
                and 0 < expires - iat <= MAX_CONNECTOR_TOKEN_TTL_SECONDS
            ):
                raise ValueError
            if set(claims) == _CLAIMS:
                tenant_id = _canonical_uuid(claims["tenant_id"])
                device_id = _canonical_uuid(claims["device_id"])
                if (
                    claims["scope"] != CONNECTOR_TOKEN_SCOPE
                    or self._device_authority is None
                ):
                    raise ValueError
                identity = ConnectorIdentity(
                    tenant_id=tenant_id,
                    device_id=device_id,
                    token_issued_at=iat,
                    token_not_before=nbf,
                    token_expires_at=expires,
                )
                return await asyncio.to_thread(self._authorize_legacy, identity)
            if set(claims) != _V1_CLAIMS or self._device_authority is None:
                raise ValueError
            tenant_id = _canonical_uuid(claims["tenant_id"])
            device_id = _canonical_uuid(claims["device_id"])
            credential_id = _canonical_uuid(claims["credential_id"])
            agent_id = _canonical_uuid(claims["agent_id"])
            token_id = _canonical_uuid(claims["jti"])
            scopes = _connector_scopes(claims["scopes"])
            identity = ConnectorIdentity(
                tenant_id=tenant_id,
                device_id=device_id,
                credential_id=credential_id,
                agent_id=agent_id,
                scopes=scopes,
                token_id=token_id,
                legacy_seed=False,
                token_issued_at=iat,
                token_not_before=nbf,
                token_expires_at=expires,
            )
            await asyncio.to_thread(self._authorize, identity)
        except Exception:  # noqa: BLE001 - untrusted token boundary is fail-closed.
            raise PermissionError("connector token rejected") from None
        return identity

    async def revalidate(self, identity: ConnectorIdentity) -> None:
        try:
            self._assert_token_active(identity)
            if identity.legacy_seed:
                await asyncio.to_thread(self._authorize_legacy, identity)
            else:
                await asyncio.to_thread(self._authorize, identity)
        except ConnectorAuthenticationExpired:
            raise
        except DeviceAuthorizationRevoked:
            raise ConnectorAuthorizationRevoked from None
        except DeviceAuthorizationSuspended:
            raise ConnectorAuthorizationSuspended from None
        except Exception:  # noqa: BLE001 - lifecycle changes fail closed
            raise ConnectorAuthorizationUnavailable from None

    async def check(self) -> None:
        self._now_seconds()

    def _now_seconds(self) -> int:
        current = self._utc_now()
        if current.utcoffset() is None:
            raise RuntimeError("connector authentication clock is invalid")
        return int(current.astimezone(UTC).timestamp())

    def _assert_token_active(self, identity: ConnectorIdentity) -> None:
        issued_at = identity.token_issued_at
        not_before = identity.token_not_before
        expires_at = identity.token_expires_at
        if (
            type(issued_at) is not int
            or type(not_before) is not int
            or type(expires_at) is not int
        ):
            raise ValueError
        now = self._now_seconds()
        if now >= expires_at:
            raise ConnectorAuthenticationExpired("connector token expired")
        if not (
            issued_at <= not_before <= now
            and 0 < expires_at - issued_at <= MAX_CONNECTOR_TOKEN_TTL_SECONDS
        ):
            raise ValueError

    def _authorize(self, identity: ConnectorIdentity) -> None:
        if (
            self._device_authority is None
            or identity.credential_id is None
            or identity.agent_id is None
        ):
            raise ValueError
        snapshot = self._device_authority.active_device_binding(
            tenant_id=UUID(identity.tenant_id),
            device_id=UUID(identity.device_id),
            credential_id=UUID(identity.credential_id),
            now=self._utc_now(),
        )
        binding = getattr(snapshot, "binding", None)
        if (
            binding is None
            or str(getattr(binding, "tenant_id", "")) != identity.tenant_id
            or str(getattr(binding, "device_id", "")) != identity.device_id
            or str(getattr(binding, "credential_id", "")) != identity.credential_id
            or str(getattr(binding, "agent_id", "")) != identity.agent_id
            or tuple(getattr(binding, "scopes", ())) != identity.scopes
        ):
            raise ValueError

    def _authorize_legacy(self, identity: ConnectorIdentity) -> ConnectorIdentity:
        if self._device_authority is None:
            raise ValueError
        snapshot = self._device_authority.active_legacy_device_binding(
            tenant_id=UUID(identity.tenant_id),
            device_id=UUID(identity.device_id),
            now=self._utc_now(),
        )
        binding = getattr(snapshot, "binding", None)
        scopes = tuple(getattr(binding, "scopes", ()))
        if (
            binding is None
            or str(getattr(binding, "tenant_id", "")) != identity.tenant_id
            or str(getattr(binding, "device_id", "")) != identity.device_id
            or getattr(binding, "credential_id", None) is None
            or getattr(binding, "agent_id", None) is None
            or "session.observe" not in scopes
        ):
            raise ValueError
        authorized = ConnectorIdentity(
            tenant_id=identity.tenant_id,
            device_id=identity.device_id,
            credential_id=str(binding.credential_id),
            agent_id=str(binding.agent_id),
            scopes=("session.observe",),
            token_id=None,
            legacy_seed=True,
            token_issued_at=identity.token_issued_at,
            token_not_before=identity.token_not_before,
            token_expires_at=identity.token_expires_at,
        )
        if identity.credential_id is not None and authorized != identity:
            raise ValueError
        return authorized

    def __repr__(self) -> str:
        return "HmacJwtConnectorAuthenticator(<redacted>)"


def build_connector_authenticator(
    environment: Mapping[str, str],
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    device_authority: ConnectorDeviceAuthority | None = None,
) -> HmacJwtConnectorAuthenticator:
    path = environment.get("HERMES_CONNECTOR_SIGNING_SECRET_FILE", "")
    secret = read_connector_signing_secret(path)
    return HmacJwtConnectorAuthenticator(
        secret,
        utc_now=utc_now,
        device_authority=device_authority,
    )


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return value


def _connector_scopes(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 2
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError
    scopes = tuple(value)
    if len(scopes) != len(set(scopes)) or not set(scopes) <= {
        "session.observe",
        "session.control.request",
    }:
        raise ValueError
    return scopes
