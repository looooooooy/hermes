"""Authentication and projection orchestration without transport or database types."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from hermes_cloud.modules.cloud_api.domain import (
    CloudApiSettings,
    IssuedAuthentication,
    IssuedControlTicket,
    IssuedObserverTicket,
    Principal,
    SensitiveToken,
    WebSocketTicketAuthentication,
    is_canonical_rfc4122_uuid_v1_to_v5,
)
from hermes_cloud.modules.cloud_api.ports import (
    LoginTenantResolverPort,
    SecretResolverPort,
)
from hermes_cloud.modules.identity.domain import (
    Argon2PasswordHasher,
    RefreshSession,
    RefreshSessionUnavailable,
    WebSocketTicket,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
    sha256_token_digest,
)
from hermes_cloud.modules.identity.ports import (
    IdentityRepositoryFailure,
    IdentityRepositoryPort,
)


class AuthenticationFailed(RuntimeError):
    """Authentication material is absent, invalid, expired, or unavailable."""


class LogoutFailed(RuntimeError):
    """A valid browser logout could not be durably completed."""


class CloudApiService:
    def __init__(
        self,
        *,
        identity_repository: IdentityRepositoryPort,
        tenant_resolver: LoginTenantResolverPort,
        secret_resolver: SecretResolverPort,
        settings: CloudApiSettings,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._identity_repository = identity_repository
        self._tenant_resolver = tenant_resolver
        self._secret_resolver = secret_resolver
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))
        self._password_hasher = Argon2PasswordHasher()

    @property
    def access_ttl_seconds(self) -> int:
        return self._settings.access_ttl_seconds

    @property
    def trusted_forwarded_proxy_hosts(self) -> tuple[str, ...]:
        return self._settings.trusted_forwarded_proxy_hosts

    def issue_password_login(
        self,
        *,
        provider: str,
        subject: str,
        password: str,
        next_path: str,
    ) -> IssuedAuthentication:
        if (
            provider != "basic"
            or next_path != ""
            or not isinstance(subject, str)
            or not 1 <= len(subject) <= 254
            or not isinstance(password, str)
            or not 1 <= len(password) <= 1024
        ):
            raise AuthenticationFailed
        tenant_id = self._tenant_resolver.tenant_for_subject(subject)
        if tenant_id is None:
            raise AuthenticationFailed
        credential = self._identity_repository.credential_by_subject(
            tenant_id=tenant_id,
            subject=subject,
        )
        if (
            credential is None
            or credential.status != "active"
            or not self._password_hasher.verify(credential.password_hash, password)
        ):
            raise AuthenticationFailed

        now = self._now()
        refresh_session_id = uuid4()
        refresh_token = self._encode_opaque_refresh(
            tenant_id=str(tenant_id),
            user_id=str(credential.user_id),
            refresh_session_id=str(refresh_session_id),
        )
        refresh_expires_at = now + timedelta(seconds=self._settings.refresh_ttl_seconds)
        self._identity_repository.create_refresh_session(
            RefreshSession(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                user_id=credential.user_id,
                token_digest=sha256_token_digest(refresh_token.reveal()),
                rotation=0,
                created_at=now,
                rotated_at=None,
                revoked_at=None,
                expires_at=refresh_expires_at,
                retention_until=refresh_expires_at + timedelta(days=30),
            )
        )
        access_expires_at = now + timedelta(seconds=self._settings.access_ttl_seconds)
        access_token = self._encode_access_token(
            tenant_id=str(tenant_id),
            user_id=str(credential.user_id),
            refresh_session_id=str(refresh_session_id),
            issued_at=now,
            expires_at=access_expires_at,
        )
        return IssuedAuthentication(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            user_id=credential.user_id,
        )

    def rotate_refresh(
        self,
        *,
        provider: str,
        refresh_token: str,
    ) -> IssuedAuthentication:
        if (
            provider != "basic"
            or not isinstance(refresh_token, str)
            or not 1 <= len(refresh_token) <= 4096
        ):
            raise AuthenticationFailed
        tenant_id, user_id, refresh_session_id = self._decode_opaque_refresh(
            refresh_token
        )
        replacement = self._encode_opaque_refresh(
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            refresh_session_id=str(refresh_session_id),
        )
        now = self._now()
        access_expires_at = now + timedelta(seconds=self._settings.access_ttl_seconds)
        access_token = self._encode_access_token(
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            refresh_session_id=str(refresh_session_id),
            issued_at=now,
            expires_at=access_expires_at,
        )
        try:
            rotated = self._identity_repository.rotate_refresh_session(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                expected_digest=sha256_token_digest(refresh_token),
                replacement_digest=sha256_token_digest(replacement.reveal()),
                now=now,
            )
        except RefreshSessionUnavailable:
            raise AuthenticationFailed from None
        if rotated.user_id != user_id:
            raise AuthenticationFailed
        return IssuedAuthentication(
            access_token=access_token,
            refresh_token=replacement,
            access_expires_at=access_expires_at,
            user_id=user_id,
        )

    def authenticate_access(self, token: str) -> Principal:
        principal = self._decode_access_token(token)
        try:
            refresh_session = self._identity_repository.refresh_session_by_id(
                tenant_id=principal.tenant_id,
                refresh_session_id=principal.refresh_session_id,
            )
        except IdentityRepositoryFailure:
            raise AuthenticationFailed from None
        if (
            refresh_session is None
            or refresh_session.user_id != principal.user_id
            or refresh_session.revoked_at is not None
            or refresh_session.expires_at <= self._now()
        ):
            raise AuthenticationFailed
        return principal

    def logout_browser_session(
        self,
        *,
        access_token: str | None,
        refresh_token: str | None,
    ) -> None:
        if access_token is None and refresh_token is None:
            return
        access_principal = (
            None if access_token is None else self._decode_access_token(access_token)
        )
        refresh_principal = (
            None
            if refresh_token is None
            else self._decode_opaque_refresh(refresh_token)
        )
        if (
            access_principal is not None
            and refresh_principal is not None
            and (
                access_principal.tenant_id,
                access_principal.user_id,
                access_principal.refresh_session_id,
            )
            != refresh_principal
        ):
            raise AuthenticationFailed
        if access_principal is not None:
            tenant_id = access_principal.tenant_id
            user_id = access_principal.user_id
            refresh_session_id = access_principal.refresh_session_id
        else:
            assert refresh_principal is not None
            tenant_id, user_id, refresh_session_id = refresh_principal
        try:
            current = self._identity_repository.refresh_session_by_id(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
            )
            if current is None or current.revoked_at is not None:
                return
            if current.user_id != user_id:
                raise AuthenticationFailed
            try:
                self._identity_repository.revoke_refresh_session(
                    tenant_id=tenant_id,
                    refresh_session_id=refresh_session_id,
                    now=self._now(),
                )
            except RefreshSessionUnavailable:
                current = self._identity_repository.refresh_session_by_id(
                    tenant_id=tenant_id,
                    refresh_session_id=refresh_session_id,
                )
                if current is None or current.revoked_at is not None:
                    return
                raise LogoutFailed from None
        except AuthenticationFailed:
            raise
        except LogoutFailed:
            raise
        except IdentityRepositoryFailure:
            raise LogoutFailed from None

    def _decode_access_token(self, token: str) -> Principal:
        if not token:
            raise AuthenticationFailed
        try:
            claims = jwt.decode(
                token,
                self._signing_key(),
                algorithms=["HS256"],
                options={"require": ["exp", "iat", "nbf"]},
            )
            if not isinstance(claims, dict) or set(claims) != {
                "tenant_id",
                "user_id",
                "provider",
                "refresh_session_id",
                "iat",
                "nbf",
                "exp",
            }:
                raise AuthenticationFailed
            if claims["provider"] != "basic":
                raise AuthenticationFailed
            issued_at = claims["iat"]
            not_before = claims["nbf"]
            expires_at = int(claims["exp"])
            if (
                type(issued_at) is not int
                or type(not_before) is not int
                or type(claims["exp"]) is not int
                or not (
                    issued_at <= not_before < expires_at
                    and 0 < expires_at - issued_at <= 3_600
                )
            ):
                raise AuthenticationFailed
            principal = Principal(
                tenant_id=UUID(claims["tenant_id"]),
                user_id=UUID(claims["user_id"]),
                provider=claims["provider"],
                refresh_session_id=UUID(claims["refresh_session_id"]),
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise AuthenticationFailed from None
        return principal

    def mint_observer_ticket(
        self,
        principal: Principal,
        *,
        client_instance_id: str | None = None,
        observer_contract: int = 1,
        agent_id: UUID | None = None,
    ) -> IssuedObserverTicket:
        if client_instance_id is not None and not is_canonical_rfc4122_uuid_v1_to_v5(
            client_instance_id
        ):
            raise AuthenticationFailed from None
        if observer_contract not in {1, 2} or isinstance(observer_contract, bool):
            raise AuthenticationFailed from None
        if observer_contract == 2 and client_instance_id is None:
            raise AuthenticationFailed from None
        if agent_id is not None and (
            not isinstance(agent_id, UUID) or client_instance_id is None
        ):
            raise AuthenticationFailed from None
        raw_ticket = self._encode_opaque_ticket(
            principal,
            client_instance_id=client_instance_id,
            observer_contract=observer_contract,
            agent_id=agent_id,
        )
        now = self._now()
        expires_at = now + timedelta(seconds=self._settings.ticket_ttl_seconds)
        self._identity_repository.issue_websocket_ticket(
            WebSocketTicket(
                tenant_id=principal.tenant_id,
                ticket_id=uuid4(),
                ticket_digest=sha256_token_digest(raw_ticket.reveal()),
                principal_type="user",
                principal_id=principal.user_id,
                refresh_session_id=principal.refresh_session_id,
                session_id=None,
                observer_scope=_observer_scope(
                    client_instance_id,
                    observer_contract,
                    agent_id,
                ),
                issued_at=now,
                expires_at=expires_at,
                consumed_at=None,
                retention_until=expires_at + timedelta(days=30),
            )
        )
        return IssuedObserverTicket(
            ticket=raw_ticket,
            ttl_seconds=self._settings.ticket_ttl_seconds,
        )

    def mint_control_ticket(
        self,
        principal: Principal,
        *,
        client_instance_id: str,
        session_id: UUID,
        profile: str,
        agent_id: UUID,
    ) -> IssuedControlTicket:
        if not is_canonical_rfc4122_uuid_v1_to_v5(client_instance_id):
            raise AuthenticationFailed from None
        if (
            not isinstance(agent_id, UUID)
            or not isinstance(session_id, UUID)
            or not isinstance(profile, str)
            or not 1 <= len(profile) <= 128
            or profile != profile.strip()
        ):
            raise AuthenticationFailed
        raw_ticket = self._encode_control_ticket(
            principal,
            client_instance_id=client_instance_id,
            session_id=session_id,
            profile=profile,
            agent_id=agent_id,
        )
        now = self._now()
        expires_at = now + timedelta(seconds=self._settings.ticket_ttl_seconds)
        self._identity_repository.issue_websocket_ticket(
            WebSocketTicket(
                tenant_id=principal.tenant_id,
                ticket_id=uuid4(),
                ticket_digest=sha256_token_digest(raw_ticket.reveal()),
                principal_type="user",
                principal_id=principal.user_id,
                refresh_session_id=principal.refresh_session_id,
                session_id=session_id,
                observer_scope=_control_scope(
                    principal.provider,
                    client_instance_id,
                    profile,
                    agent_id,
                ),
                issued_at=now,
                expires_at=expires_at,
                consumed_at=None,
                retention_until=expires_at + timedelta(days=30),
            )
        )
        return IssuedControlTicket(
            ticket=raw_ticket,
            ttl_seconds=self._settings.ticket_ttl_seconds,
        )

    def consume_observer_ticket(self, raw_ticket: str) -> Principal:
        authenticated = self.consume_websocket_ticket(raw_ticket)
        if authenticated.connection_role != "observer":
            raise AuthenticationFailed
        return authenticated.principal

    def consume_websocket_ticket(
        self,
        raw_ticket: str,
    ) -> WebSocketTicketAuthentication:
        authenticated = self._decode_opaque_ticket(raw_ticket)
        principal = authenticated.principal
        try:
            consumed = self._identity_repository.consume_websocket_ticket(
                WebSocketTicketClaim(
                    tenant_id=principal.tenant_id,
                    ticket_digest=sha256_token_digest(raw_ticket),
                    principal_type="user",
                    principal_id=principal.user_id,
                    refresh_session_id=principal.refresh_session_id,
                    session_id=authenticated.session_id,
                ),
                now=self._now(),
            )
        except WebSocketTicketUnavailable:
            raise AuthenticationFailed from None
        expected_scope = (
            _observer_scope(
                authenticated.client_instance_id,
                authenticated.observer_contract,
                authenticated.agent_id,
            )
            if authenticated.connection_role == "observer"
            else _control_scope(
                principal.provider,
                authenticated.client_instance_id,
                authenticated.profile,
                authenticated.agent_id,
            )
        )
        if consumed.observer_scope != expected_scope:
            raise AuthenticationFailed
        return authenticated

    def _signing_key(self) -> bytes:
        key = self._secret_resolver.resolve(self._settings.signing_secret_ref)
        if len(key) < 32:
            raise AuthenticationFailed
        return key

    def _encode_access_token(
        self,
        *,
        tenant_id: str,
        user_id: str,
        refresh_session_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> SensitiveToken:
        issued_at_seconds = int(issued_at.timestamp())
        token = jwt.encode(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "provider": "basic",
                "refresh_session_id": refresh_session_id,
                "iat": issued_at_seconds,
                "nbf": issued_at_seconds,
                "exp": int(expires_at.timestamp()),
            },
            self._signing_key(),
            algorithm="HS256",
        )
        return SensitiveToken(token)

    def _encode_opaque_refresh(
        self,
        *,
        tenant_id: str,
        user_id: str,
        refresh_session_id: str,
    ) -> SensitiveToken:
        payload = json.dumps(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "refresh_session_id": refresh_session_id,
                "nonce": secrets.token_urlsafe(32),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = _base64url(payload)
        signature = _base64url(
            hmac.digest(self._signing_key(), encoded.encode(), hashlib.sha256)
        )
        return SensitiveToken(f"{encoded}.{signature}")

    def _encode_opaque_ticket(
        self,
        principal: Principal,
        *,
        client_instance_id: str | None,
        observer_contract: int,
        agent_id: UUID | None,
    ) -> SensitiveToken:
        claims: dict[str, object] = {
            "tenant_id": str(principal.tenant_id),
            "principal_id": str(principal.user_id),
            "refresh_session_id": str(principal.refresh_session_id),
            "session_key": None,
            "nonce": secrets.token_urlsafe(32),
        }
        if client_instance_id is not None:
            claims.update(
                {
                    "connection_role": "observer",
                    "provider": principal.provider,
                    "client_instance_id": client_instance_id,
                    "profile": None,
                }
            )
        if observer_contract == 2:
            claims["observer_contract"] = 2
        if agent_id is not None:
            claims["agent_id"] = str(agent_id)
        payload = json.dumps(
            claims,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = _base64url(payload)
        signature = _base64url(
            hmac.digest(self._signing_key(), encoded.encode(), hashlib.sha256)
        )
        return SensitiveToken(f"{encoded}.{signature}")

    def _encode_control_ticket(
        self,
        principal: Principal,
        *,
        client_instance_id: str,
        session_id: UUID,
        profile: str,
        agent_id: UUID,
    ) -> SensitiveToken:
        claims: dict[str, object] = {
                "tenant_id": str(principal.tenant_id),
                "principal_id": str(principal.user_id),
                "refresh_session_id": str(principal.refresh_session_id),
                "connection_role": "control",
                "provider": principal.provider,
                "client_instance_id": client_instance_id,
                "session_id": str(session_id),
                "profile": profile,
                "nonce": secrets.token_urlsafe(32),
            }
        claims["agent_id"] = str(agent_id)
        payload = json.dumps(
            claims,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = _base64url(payload)
        signature = _base64url(
            hmac.digest(self._signing_key(), encoded.encode(), hashlib.sha256)
        )
        return SensitiveToken(f"{encoded}.{signature}")

    def _decode_opaque_ticket(
        self,
        token: str,
    ) -> WebSocketTicketAuthentication:
        try:
            encoded, signature = token.split(".", 1)
            expected = _base64url(
                hmac.digest(self._signing_key(), encoded.encode(), hashlib.sha256)
            )
            if not hmac.compare_digest(signature, expected):
                raise AuthenticationFailed
            payload = json.loads(_unbase64url(encoded))
            observer_fields = {
                "tenant_id",
                "principal_id",
                "refresh_session_id",
                "session_key",
                "nonce",
            }
            observer_client_fields = observer_fields | {
                "connection_role",
                "provider",
                "client_instance_id",
                "profile",
            }
            observer_agent_fields = observer_client_fields | {"agent_id"}
            control_session_fields = (
                observer_client_fields - {"session_key"}
            ) | {"session_id", "agent_id"}
            observer_v2_fields = observer_client_fields | {"observer_contract"}
            observer_v2_agent_fields = observer_v2_fields | {"agent_id"}
            fields = frozenset(payload) if isinstance(payload, dict) else frozenset()
            if not isinstance(payload, dict) or fields not in {
                frozenset(observer_fields),
                frozenset(observer_client_fields),
                frozenset(observer_agent_fields),
                frozenset(control_session_fields),
                frozenset(observer_v2_fields),
                frozenset(observer_v2_agent_fields),
            }:
                raise AuthenticationFailed
            if not isinstance(payload["nonce"], str) or not payload["nonce"]:
                raise AuthenticationFailed
            provider = payload.get("provider", "basic")
            if provider != "basic":
                raise AuthenticationFailed
            principal = Principal(
                tenant_id=UUID(payload["tenant_id"]),
                user_id=UUID(payload["principal_id"]),
                provider=provider,
                refresh_session_id=UUID(payload["refresh_session_id"]),
            )
            if fields == observer_fields:
                if payload["session_key"] is not None:
                    raise AuthenticationFailed
                return WebSocketTicketAuthentication(
                    principal=principal,
                    connection_role="observer",
                    client_instance_id=None,
                    session_id=None,
                    session_key=None,
                    profile=None,
                    agent_id=None,
                    observer_contract=1,
                )
            client_instance_id = payload["client_instance_id"]
            profile = payload["profile"]
            observer_contract = payload.get("observer_contract", 1)
            if payload["connection_role"] == "observer":
                session_key = payload["session_key"]
                agent_id = None
                if "agent_id" in payload:
                    agent_text = payload["agent_id"]
                    if not is_canonical_rfc4122_uuid_v1_to_v5(agent_text):
                        raise AuthenticationFailed
                    agent_id = UUID(agent_text)
                if (
                    not is_canonical_rfc4122_uuid_v1_to_v5(client_instance_id)
                    or session_key is not None
                    or profile is not None
                    or observer_contract not in {1, 2}
                    or isinstance(observer_contract, bool)
                ):
                    raise AuthenticationFailed
                return WebSocketTicketAuthentication(
                    principal=principal,
                    connection_role="observer",
                    client_instance_id=client_instance_id,
                    session_id=None,
                    session_key=None,
                    profile=None,
                    agent_id=agent_id,
                    observer_contract=observer_contract,
                )
            if (
                payload["connection_role"] != "control"
                or fields != frozenset(control_session_fields)
                or not is_canonical_rfc4122_uuid_v1_to_v5(client_instance_id)
                or not is_canonical_rfc4122_uuid_v1_to_v5(payload["session_id"])
                or not isinstance(profile, str)
                or not 1 <= len(profile) <= 128
                or profile != profile.strip()
            ):
                raise AuthenticationFailed
            agent_text = payload["agent_id"]
            if not is_canonical_rfc4122_uuid_v1_to_v5(agent_text):
                raise AuthenticationFailed
            agent_id = UUID(agent_text)
            return WebSocketTicketAuthentication(
                principal=principal,
                connection_role="control",
                client_instance_id=client_instance_id,
                session_id=UUID(payload["session_id"]),
                session_key=None,
                profile=profile,
                agent_id=agent_id,
                observer_contract=1,
            )
        except (KeyError, TypeError, ValueError):
            raise AuthenticationFailed from None

    def _decode_opaque_refresh(self, token: str) -> tuple[UUID, UUID, UUID]:
        try:
            encoded, signature = token.split(".", 1)
            expected = _base64url(
                hmac.digest(self._signing_key(), encoded.encode(), hashlib.sha256)
            )
            if not hmac.compare_digest(signature, expected):
                raise AuthenticationFailed
            payload = json.loads(_unbase64url(encoded))
            if not isinstance(payload, dict) or set(payload) != {
                "tenant_id",
                "user_id",
                "refresh_session_id",
                "nonce",
            }:
                raise AuthenticationFailed
            if not isinstance(payload["nonce"], str) or not payload["nonce"]:
                raise AuthenticationFailed
            return (
                UUID(payload["tenant_id"]),
                UUID(payload["user_id"]),
                UUID(payload["refresh_session_id"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise AuthenticationFailed from None


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unbase64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _control_scope(
    provider: str,
    client_instance_id: str | None,
    profile: str | None,
    agent_id: UUID | None,
) -> tuple[str, ...]:
    if (
        provider != "basic"
        or client_instance_id is None
        or profile is None
        or agent_id is None
    ):
        raise AuthenticationFailed
    return (
        "session.control",
        f"provider={provider}",
        f"client_instance_id={client_instance_id}",
        f"profile={profile}",
        f"agent_id={agent_id}",
    )


def _observer_scope(
    client_instance_id: str | None,
    observer_contract: int = 1,
    agent_id: UUID | None = None,
) -> tuple[str, ...]:
    if client_instance_id is None:
        return ("session.observe",)
    scope = (
        "session.observe",
        f"client_instance_id={client_instance_id}",
    )
    if observer_contract == 2:
        scope = (*scope, "observer_contract=2")
    if agent_id is not None:
        scope = (*scope, f"agent_id={agent_id}")
    return scope
