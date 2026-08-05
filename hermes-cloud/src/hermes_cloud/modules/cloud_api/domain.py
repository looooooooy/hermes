"""Infrastructure-neutral values for the external Cloud P0 surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from uuid import RFC_4122, UUID


def is_canonical_rfc4122_uuid_v1_to_v5(value: object) -> bool:
    """Return whether value is canonical lowercase RFC 4122 UUID text."""
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return (
        str(parsed) == value
        and parsed.variant == RFC_4122
        and parsed.version in {1, 2, 3, 4, 5}
    )


@dataclass(frozen=True, slots=True)
class CloudApiSettings:
    signing_secret_ref: str
    access_ttl_seconds: int = 300
    refresh_ttl_seconds: int = 30 * 24 * 60 * 60
    ticket_ttl_seconds: int = 60
    trusted_forwarded_proxy_hosts: tuple[str, ...] = ("127.0.0.1", "::1")

    def __post_init__(self) -> None:
        if not self.signing_secret_ref:
            raise ValueError("signing secret reference must not be empty")
        if self.access_ttl_seconds <= 0:
            raise ValueError("access TTL must be positive")
        if self.refresh_ttl_seconds <= self.access_ttl_seconds:
            raise ValueError("refresh TTL must exceed access TTL")
        if not 1 <= self.ticket_ttl_seconds <= 60:
            raise ValueError("ticket TTL is outside contract bounds")
        if len(set(self.trusted_forwarded_proxy_hosts)) != len(
            self.trusted_forwarded_proxy_hosts
        ):
            raise ValueError("trusted forwarded proxy hosts must be unique")
        try:
            trusted_hosts = tuple(
                ip_address(host) for host in self.trusted_forwarded_proxy_hosts
            )
        except ValueError:
            raise ValueError("trusted forwarded proxy host is invalid") from None
        if any(not host.is_loopback for host in trusted_hosts):
            raise ValueError("trusted forwarded proxy host must be loopback")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> CloudApiSettings:
        allowed = {
            "signing_secret_ref",
            "access_ttl_seconds",
            "refresh_ttl_seconds",
            "ticket_ttl_seconds",
            "trusted_forwarded_proxy_hosts",
        }
        if set(values) - allowed:
            raise ValueError("Cloud API settings contain unknown fields")
        return cls(
            signing_secret_ref=str(values.get("signing_secret_ref", "")),
            access_ttl_seconds=int(values.get("access_ttl_seconds", 300)),
            refresh_ttl_seconds=int(
                values.get("refresh_ttl_seconds", 30 * 24 * 60 * 60)
            ),
            ticket_ttl_seconds=int(values.get("ticket_ttl_seconds", 60)),
            trusted_forwarded_proxy_hosts=_proxy_hosts(
                values.get(
                    "trusted_forwarded_proxy_hosts",
                    ("127.0.0.1", "::1"),
                )
            ),
        )


def _proxy_hosts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(host, str) for host in value
    ):
        raise ValueError("trusted forwarded proxy hosts are invalid")
    return tuple(value)


class SensitiveToken:
    """Bearer material whose normal string representations remain redacted."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("sensitive token must not be empty")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SensitiveToken(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class IssuedAuthentication:
    access_token: SensitiveToken
    refresh_token: SensitiveToken
    access_expires_at: datetime
    user_id: UUID


@dataclass(frozen=True, slots=True)
class IssuedObserverTicket:
    ticket: SensitiveToken
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class IssuedControlTicket:
    ticket: SensitiveToken
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    provider: str
    refresh_session_id: UUID


@dataclass(frozen=True, slots=True)
class ObserverSubscription:
    subscription_id: UUID
    target_subscription_id: UUID
    session_key: str
    profile: str
    requires_initial_snapshot: bool


class ObserverSubscriptionCapacityExceeded(RuntimeError):
    """The Connector already owns its bounded set of Observer targets."""


@dataclass(frozen=True, slots=True)
class WebSocketTicketAuthentication:
    principal: Principal
    connection_role: str
    client_instance_id: str | None
    session_id: UUID | None
    session_key: str | None
    profile: str | None
    agent_id: UUID | None = None
    observer_contract: int = 1

    def __post_init__(self) -> None:
        if self.observer_contract not in {1, 2}:
            raise ValueError("observer contract is invalid")
        if self.connection_role == "observer":
            if (
                self.session_id is not None
                or self.session_key is not None
                or self.profile is not None
            ):
                raise ValueError("observer ticket cannot bind a session target")
            if self.agent_id is not None and not isinstance(self.agent_id, UUID):
                raise ValueError("observer ticket agent is invalid")
            if (
                self.client_instance_id is not None
                and not is_canonical_rfc4122_uuid_v1_to_v5(self.client_instance_id)
            ):
                raise ValueError("observer client instance is invalid")
            return
        if self.connection_role != "control":
            raise ValueError("WebSocket ticket role is invalid")
        if self.observer_contract != 1:
            raise ValueError("control ticket must remain on observer contract v1")
        if (
            self.client_instance_id is None
            or self.session_id is None
            or self.profile is None
            or self.agent_id is None
        ):
            raise ValueError("control ticket scope is incomplete")
        if self.session_key is not None and (
            not 1 <= len(self.session_key) <= 256
            or self.session_key != self.session_key.strip()
        ):
            raise ValueError("control ticket session binding is invalid")
