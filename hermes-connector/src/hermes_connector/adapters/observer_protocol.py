from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.observer import SessionEvent, SessionSnapshot

MAX_PRE_SNAPSHOT_EVENTS = 32
OUTPUT_PARITY_CAPABILITY = "session.observe.output-parity.v1"
SUBSCRIBE_ID = "connector-observer-subscribe"
UNSUBSCRIBE_ID = "connector-observer-unsubscribe"
SUBSCRIBE_V2_ID = 1
UNSUBSCRIBE_V2_ID = 2

AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]


class ObserverEndpointUnavailable(RuntimeError):
    pass


class ObserverProtocolError(RuntimeError):
    pass


class ObserverResnapshotRequired(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedObserverSubscription:
    result: Mapping[str, object]
    subscription_id: str
    events: tuple[Mapping[str, object], ...]


def observer_contract(authority: LocalRuntimeAuthority) -> int:
    capabilities = {
        *authority.required_capabilities,
        *authority.optional_capabilities,
    }
    return 2 if OUTPUT_PARITY_CAPABILITY in capabilities else 1


def subscribe_request(
    *,
    observer_contract: int,
    profile: str,
    session_key: str,
    runtime_generation: str,
) -> dict[str, object]:
    if observer_contract == 2:
        return {
            "jsonrpc": "2.0",
            "id": SUBSCRIBE_V2_ID,
            "method": "session.observe.subscribe",
            "params": {
                "observer_contract": 2,
                "session_key": session_key,
                "profile": profile,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": SUBSCRIBE_ID,
        "method": "session.observe.subscribe",
        "params": {
            "session_key": session_key,
            "profile": profile,
            "runtime_generation": runtime_generation,
            "relay_local_only": True,
        },
    }


def unsubscribe_request(
    *,
    observer_contract: int,
    subscription_id: str,
) -> dict[str, object]:
    if observer_contract == 2:
        return {
            "jsonrpc": "2.0",
            "id": UNSUBSCRIBE_V2_ID,
            "method": "session.observe.unsubscribe",
            "params": {
                "observer_contract": 2,
                "subscription_id": subscription_id,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": UNSUBSCRIBE_ID,
        "method": "session.observe.unsubscribe",
        "params": {"subscription_id": subscription_id},
    }


def ready_payload(
    frame: Mapping[str, object],
    *,
    observer_contract: int,
) -> Mapping[str, object] | None:
    if frame.get("method") != "event":
        return None
    if set(frame) != {"jsonrpc", "method", "params"} or frame.get("jsonrpc") != "2.0":
        raise ObserverProtocolError("Observer local envelope is invalid")
    params = frame.get("params")
    if not isinstance(params, dict):
        raise ObserverProtocolError("Observer ready params are invalid")
    if params.get("type") != "gateway.ready":
        return None
    if set(params) != {"type", "payload"}:
        raise ObserverProtocolError("Observer ready params contain an unknown field")
    payload = params.get("payload")
    if observer_contract == 2:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"observer_contract", "connection_role"}
            or payload.get("observer_contract") != 2
            or payload.get("connection_role") != "observer"
        ):
            raise ObserverProtocolError("Observer ready contract is invalid")
        return payload
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "observer_contract",
            "local_gateway_protocol",
            "connection_role",
            "profile",
            "runtime_generation",
            "instance_id",
        }
        or type(payload["observer_contract"]) is not int
        or payload["observer_contract"] != 1
        or type(payload["local_gateway_protocol"]) is not int
        or payload["local_gateway_protocol"] != 1
        or payload["connection_role"] != "observer"
    ):
        raise ObserverProtocolError("Observer ready contract is invalid")
    return payload


def validate_ready_identity(
    payload: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    instance_id: str,
) -> None:
    if (
        payload.get("profile") != profile
        or payload.get("runtime_generation") != runtime_generation
        or payload.get("instance_id") != instance_id
    ):
        raise ObserverResnapshotRequired(
            "Observer ready identity does not match runtime authority"
        )


def is_event_notification(frame: Mapping[str, object]) -> bool:
    return (
        set(frame) == {"jsonrpc", "method", "params"}
        and frame.get("jsonrpc") == "2.0"
        and frame.get("method") == "event"
        and isinstance(frame.get("params"), dict)
    )


def same_authority_identity(
    current: LocalRuntimeAuthority,
    expected: LocalRuntimeAuthority,
) -> bool:
    return (
        current.profile == expected.profile
        and current.runtime_generation == expected.runtime_generation
        and current.instance_id == expected.instance_id
        and current.host_bundle_id == expected.host_bundle_id
        and current.process_identity == expected.process_identity
    )


async def require_authority(
    provider: AuthorityProvider,
    *,
    expected_authority: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    authority = await provider()
    if authority is None or "session.observe" not in {
        *authority.required_capabilities,
        *authority.optional_capabilities,
    }:
        raise ObserverResnapshotRequired("Observer runtime authority is unavailable")
    if expected_authority is not None and not same_authority_identity(
        authority,
        expected_authority,
    ):
        raise ObserverResnapshotRequired("Observer runtime authority changed")
    return authority


def snapshot_from_result(
    codec: ConnectorProtocolCodec,
    result: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    session_key: str,
    observer_contract: int,
) -> SessionSnapshot:
    if observer_contract == 2:
        allowed = {
            "subscription_id",
            "observer_contract",
            "profile",
            "runtime_generation",
            "session_key",
            "runtime_session_id",
            "running",
            "status",
            "event_sequence",
            "snapshot_event_sequence",
            "messages",
            "inflight",
            "todo_sections",
            "subagents",
            "tools",
            "terminals",
            "replay_events",
            "extensions",
        }
        required = allowed - {"subscription_id", "extensions"}
        if set(result) - allowed or not required <= set(result):
            raise ObserverProtocolError(
                "Observer v2 snapshot does not match the exact result schema"
            )
        payload = {
            key: value for key, value in result.items() if key != "subscription_id"
        }
        try:
            snapshot = codec.decode_session_snapshot_v2_payload(payload)
        except ValueError as error:
            raise ObserverProtocolError(
                "Observer v2 snapshot contract is invalid"
            ) from error
        if snapshot.session_key != session_key:
            raise ObserverResnapshotRequired("Observer snapshot session does not match")
        if snapshot.profile != profile:
            raise ObserverResnapshotRequired("Observer snapshot profile does not match")
        if snapshot.runtime_generation != runtime_generation:
            raise ObserverResnapshotRequired(
                "Observer snapshot runtime authority changed"
            )
        return snapshot

    allowed = {
        "subscription_id",
        "profile",
        "runtime_generation",
        "session_key",
        "runtime_session_id",
        "running",
        "status",
        "event_sequence",
        "snapshot_event_sequence",
        "messages",
        "inflight",
        "replay_events",
    }
    if set(result) - allowed:
        raise ObserverProtocolError("Observer snapshot contains an unexpected field")
    required = allowed - {"subscription_id"}
    if not required <= set(result):
        raise ObserverProtocolError("Observer snapshot is missing an explicit field")
    if result.get("session_key") != session_key:
        raise ObserverResnapshotRequired("Observer snapshot session does not match")
    if result["profile"] != profile:
        raise ObserverResnapshotRequired("Observer snapshot profile does not match")
    if result.get("runtime_generation") != runtime_generation:
        raise ObserverResnapshotRequired("Observer snapshot runtime authority changed")
    payload = {key: value for key, value in result.items() if key != "subscription_id"}
    return codec.decode_session_snapshot_payload(payload)


def session_event_from_frame(
    codec: ConnectorProtocolCodec,
    frame: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    observer_contract: int = 1,
) -> SessionEvent:
    if not is_event_notification(frame):
        raise ObserverProtocolError("Observer live frame is invalid")
    params = frame.get("params")
    if not isinstance(params, dict):
        raise ObserverProtocolError("Observer event params are invalid")
    if params.get("profile") != profile:
        raise ObserverResnapshotRequired("Observer event profile does not match")
    if params.get("runtime_generation") != runtime_generation:
        raise ObserverResnapshotRequired(
            "Observer event runtime authority does not match"
        )
    payload = dict(params)
    try:
        if observer_contract == 2:
            return codec.decode_session_event_v2_payload(payload)
        return codec.decode_session_event_payload(payload)
    except ValueError as error:
        raise ObserverProtocolError("Observer live event contract is invalid") from error


__all__ = [
    "MAX_PRE_SNAPSHOT_EVENTS",
    "ObserverEndpointUnavailable",
    "ObserverProtocolError",
    "ObserverResnapshotRequired",
    "PreparedObserverSubscription",
    "is_event_notification",
    "observer_contract",
    "ready_payload",
    "require_authority",
    "same_authority_identity",
    "session_event_from_frame",
    "snapshot_from_result",
    "subscribe_request",
    "unsubscribe_request",
    "validate_ready_identity",
]
