from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.observer_protocol import (
    ObserverProtocolError,
    same_authority_identity,
)
from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.session_catalog import (
    LocalSessionCatalogPage,
    SessionCatalogEvent,
    SessionCatalogResnapshotRequired,
)

MAX_CATALOG_EVENT_BUFFER = 1_024
MAX_CATALOG_PAGE_SIZE = 128
CATALOG_CAPABILITY = "session.catalog.v1"

AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]
RequestIdFactory = Callable[[], UUID | str]


class SessionCatalogProtocolError(ObserverProtocolError):
    pass


async def require_catalog_authority(
    provider: AuthorityProvider,
    *,
    profile: str,
    runtime_generation: str,
    expected: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    authority = await provider()
    capabilities = (
        {*authority.required_capabilities, *authority.optional_capabilities}
        if authority is not None
        else set()
    )
    if (
        authority is None
        or authority.profile != profile
        or authority.runtime_generation != runtime_generation
        or CATALOG_CAPABILITY not in capabilities
    ):
        raise SessionCatalogResnapshotRequired(
            "catalog runtime authority is unavailable"
        )
    if expected is not None and not same_authority_identity(authority, expected):
        raise SessionCatalogResnapshotRequired("catalog runtime authority changed")
    return authority


def response_result(frame: Mapping[str, object]) -> Mapping[str, object]:
    if frame.get("jsonrpc") != "2.0" or set(frame) not in (
        {"jsonrpc", "id", "result"},
        {"jsonrpc", "id", "error"},
    ):
        raise SessionCatalogProtocolError("catalog response envelope is invalid")
    if "error" in frame:
        error = frame["error"]
        if (
            isinstance(error, dict)
            and set(error) == {"code", "message", "reason"}
            and error.get("code") == 4400
            and error.get("message") == "session catalog reset required"
        ):
            raise SessionCatalogResnapshotRequired("catalog reset required")
        raise SessionCatalogProtocolError("catalog RPC error is invalid")
    result = frame.get("result")
    if not isinstance(result, dict):
        raise SessionCatalogProtocolError("catalog response result is invalid")
    return result


def buffer_notification(
    frame: Mapping[str, object],
    pending: list[Mapping[str, object]],
) -> None:
    if frame.get("jsonrpc") != "2.0" or set(frame) != {
        "jsonrpc",
        "method",
        "params",
    }:
        raise SessionCatalogProtocolError("catalog notification is invalid")
    if frame.get("method") == "session.catalog.reset_required":
        raise SessionCatalogResnapshotRequired("catalog reset required")
    if frame.get("method") != "session.catalog.event":
        raise SessionCatalogProtocolError("catalog notification method is invalid")
    if len(pending) >= MAX_CATALOG_EVENT_BUFFER:
        raise SessionCatalogResnapshotRequired("catalog event buffer overflow")
    pending.append(frame)


def local_page(
    codec: ConnectorProtocolCodec,
    result: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    page_index: int,
) -> LocalSessionCatalogPage:
    required = {
        "subscription_id",
        "snapshot_id",
        "profile",
        "runtime_generation",
        "catalog_revision",
        "page_index",
        "is_last",
        "sessions",
        "next_cursor",
    }
    if set(result) != required:
        raise SessionCatalogProtocolError("catalog page shape is invalid")
    payload = {
        key: value
        for key, value in result.items()
        if key not in {"subscription_id", "next_cursor"}
    }
    try:
        page = codec.decode_session_catalog_snapshot_page_payload(payload)
        subscription_id = canonical_uuid(result["subscription_id"])
    except (TypeError, ValueError) as error:
        raise SessionCatalogProtocolError("catalog page contract is invalid") from error
    next_cursor = result["next_cursor"]
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 512
    ):
        raise SessionCatalogProtocolError("catalog page cursor is invalid")
    if (
        page.profile != profile
        or page.runtime_generation != runtime_generation
        or page.page_index != page_index
        or (page.is_last and next_cursor is not None)
        or (not page.is_last and (not page.sessions or next_cursor is None))
    ):
        raise SessionCatalogResnapshotRequired("catalog page authority changed")
    return LocalSessionCatalogPage(
        subscription_id=subscription_id,
        snapshot_id=page.snapshot_id,
        profile=page.profile,
        runtime_generation=page.runtime_generation,
        catalog_revision=page.catalog_revision,
        page_index=page.page_index,
        is_last=page.is_last,
        sessions=page.sessions,
        next_cursor=next_cursor,
    )


def catalog_event(
    codec: ConnectorProtocolCodec,
    frame: Mapping[str, object],
    *,
    subscription_id: UUID,
    profile: str,
    runtime_generation: str,
) -> SessionCatalogEvent:
    if frame.get("method") == "session.catalog.reset_required":
        raise SessionCatalogResnapshotRequired("catalog reset required")
    if (
        frame.get("jsonrpc") != "2.0"
        or frame.get("method") != "session.catalog.event"
    ):
        raise SessionCatalogProtocolError("catalog event envelope is invalid")
    params = frame.get("params")
    if not isinstance(params, dict) or set(params) != {
        "subscription_id",
        "profile",
        "runtime_generation",
        "catalog_sequence",
        "action",
        "entry",
    }:
        raise SessionCatalogProtocolError("catalog event shape is invalid")
    try:
        observed_subscription = canonical_uuid(params["subscription_id"])
        event = codec.decode_session_catalog_event_payload(
            {
                key: value
                for key, value in params.items()
                if key != "subscription_id"
            }
        )
    except (TypeError, ValueError) as error:
        raise SessionCatalogProtocolError("catalog event contract is invalid") from error
    if (
        observed_subscription != subscription_id
        or event.profile != profile
        or event.runtime_generation != runtime_generation
    ):
        raise SessionCatalogResnapshotRequired("catalog event authority changed")
    return event


def request_id(factory: RequestIdFactory) -> str:
    return str(canonical_uuid(factory()))


__all__ = [
    "CATALOG_CAPABILITY",
    "MAX_CATALOG_EVENT_BUFFER",
    "MAX_CATALOG_PAGE_SIZE",
    "SessionCatalogProtocolError",
    "buffer_notification",
    "catalog_event",
    "local_page",
    "request_id",
    "require_catalog_authority",
    "response_result",
]
