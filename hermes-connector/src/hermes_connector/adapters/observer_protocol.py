from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.domain.identifiers import canonical_uuid
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
    if frame.get("jsonrpc") != "2.0" or frame.get("method") != "event":
        return None
    params = frame.get("params")
    if not isinstance(params, Mapping) or params.get("type") != "gateway.ready":
        return None
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        raise ObserverProtocolError("Observer ready payload is invalid")
    if observer_contract == 2:
        if set(payload) != {"observer_contract", "connection_role"}:
            raise ObserverProtocolError("Observer v2 ready payload is invalid")
        if payload.get("observer_contract") != 2:
            raise ObserverProtocolError("Observer v2 contract marker is invalid")
        if payload.get("connection_role") != "observer":
            raise ObserverProtocolError("Observer connection role is invalid")
        return payload
    if set(payload) != {
        "local_gateway_protocol",
        "observer_contract",
        "connection_role",
        "profile",
        "runtime_generation",
        "instance_id",
    }:
        raise ObserverProtocolError("Observer ready payload is invalid")
    if payload.get("observer_contract") != 1:
        raise ObserverProtocolError("Observer v1 contract marker is invalid")
    if payload.get("local_gateway_protocol") != 1:
        raise ObserverProtocolError("Observer local protocol marker is invalid")
    if payload.get("connection_role") != "observer":
        raise ObserverProtocolError("Observer connection role is invalid")
    return payload


def validate_ready_identity(
    ready: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    instance_id: str,
) -> None:
    if ready.get("profile") != profile:
        raise ObserverResnapshotRequired("Observer ready profile authority changed")
    if ready.get("runtime_generation") != runtime_generation:
        raise ObserverResnapshotRequired("Observer ready runtime authority changed")
    try:
        ready_instance = str(canonical_uuid(ready.get("instance_id")))
        expected_instance = str(canonical_uuid(instance_id))
    except (TypeError, ValueError):
        raise ObserverProtocolError("Observer ready instance id is invalid") from None
    if ready_instance != expected_instance:
        raise ObserverResnapshotRequired("Observer ready instance authority changed")


def is_event_notification(frame: Mapping[str, object]) -> bool:
    return (
        frame.get("jsonrpc") == "2.0"
        and frame.get("method") == "event"
        and "id" not in frame
    )


def same_authority_identity(
    left: LocalRuntimeAuthority,
    right: LocalRuntimeAuthority,
) -> bool:
    return (
        left.profile == right.profile
        and left.runtime_generation == right.runtime_generation
        and left.instance_id == right.instance_id
        and left.host_bundle_id == right.host_bundle_id
        and left.process_identity == right.process_identity
        and left.required_capabilities == right.required_capabilities
        and left.optional_capabilities == right.optional_capabilities
    )


async def require_authority(
    provider: AuthorityProvider,
    *,
    expected_authority: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    try:
        authority = await provider()
    except BaseException as error:
        raise ObserverResnapshotRequired("Observer runtime authority is unavailable") from error
    if authority is None:
        raise ObserverResnapshotRequired("Observer runtime authority is unavailable")
    capabilities = {
        *authority.required_capabilities,
        *authority.optional_capabilities,
    }
    if "session.observe" not in capabilities:
        raise ObserverResnapshotRequired("Observer capability is unavailable")
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
    try:
        snapshot = codec.decode_session_snapshot(
            result,
            profile=profile,
            runtime_generation=runtime_generation,
            session_key=session_key,
            observer_contract=observer_contract,
        )
    except (TypeError, ValueError) as error:
        raise ObserverProtocolError("Observer snapshot is invalid") from error
    if snapshot.runtime_generation != runtime_generation:
        raise ObserverResnapshotRequired("Observer snapshot runtime authority changed")
    if snapshot.profile != profile or snapshot.session_key != session_key:
        raise ObserverProtocolError("Observer snapshot binding is invalid")
    return snapshot


def session_event_from_frame(
    codec: ConnectorProtocolCodec,
    frame: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    observer_contract: int,
) -> SessionEvent:
    if frame.get("jsonrpc") != "2.0" or frame.get("method") != "event":
        raise ObserverProtocolError("Observer event envelope is invalid")
    params = frame.get("params")
    if not isinstance(params, Mapping):
        raise ObserverProtocolError("Observer event params are invalid")
    try:
        event = codec.decode_session_event(
            params,
            profile=profile,
            runtime_generation=runtime_generation,
            observer_contract=observer_contract,
        )
    except (TypeError, ValueError) as error:
        raise ObserverProtocolError("Observer event payload is invalid") from error
    if event.runtime_generation != runtime_generation or event.profile != profile:
        raise ObserverResnapshotRequired("Observer event runtime authority changed")
    return event


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
