"""ControlRuntimePort adapter for the process-local owner-control broker."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hermes_cloud.contracts.mobile_control import CONTROL_ERROR_CODES
from hermes_cloud.modules.control.broker import OwnerControlBroker
from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
    ControlRpcError,
)
from hermes_cloud.modules.control.ports import ControlRouteResolverPort

_OWNER_ACTION_METHODS = frozenset(
    {
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    }
)


class BrokeredControlRuntime:
    available_methods = (
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "session.command.status",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    )
    error_codes: Mapping[str, int] = CONTROL_ERROR_CODES

    def __init__(
        self,
        *,
        broker: OwnerControlBroker,
        route_resolver: ControlRouteResolverPort,
        request_id_factory: Callable[[], UUID] = uuid4,
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("control timeout must be finite and positive")
        self._broker = broker
        self._route_resolver = route_resolver
        self._request_id_factory = request_id_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._timeout_seconds = timeout_seconds
        self._routes: dict[str, ControlConnectorRoute] = {}

    async def open(self, *, context: ControlRequestContext) -> None:
        authentication = context.authentication
        route = await self._route_resolver.resolve(context)
        authority_tenant_id = route.principal_tenant_id or route.tenant_id
        if authority_tenant_id != str(authentication.principal.tenant_id):
            raise ControlRpcError(
                code=4202,
                message="authoritative live runtime unavailable",
            )
        issued_at, expires_at = self._request_window()
        await self._broker.open_transport(
            control_connection_id=context.connection_id,
            route=route,
            request_id=str(self._request_id_factory()),
            issued_at=issued_at,
            expires_at=expires_at,
            body={
                "principal_id": str(authentication.principal.user_id),
                "client_instance_id": authentication.client_instance_id,
                "session_key": authentication.session_key,
                "profile": authentication.profile,
            },
            timeout_seconds=self._timeout_seconds,
        )
        self._routes[context.connection_id] = route

    async def execute(
        self,
        *,
        context: ControlRequestContext,
        method: str,
        params: dict[str, object],
    ) -> Mapping[str, object]:
        if method not in self.available_methods:
            raise ControlRpcError(
                code=4209,
                message="method not allowed for this control slice",
            )
        body = self._operation_body(method, params)
        self._verify_ticket_binding(context, params)
        route = await self._route_resolver.resolve(context)
        opened_route = self._routes.get(context.connection_id)
        authority_tenant_id = route.principal_tenant_id or route.tenant_id
        if (
            opened_route is None
            or route != opened_route
            or authority_tenant_id
            != str(context.authentication.principal.tenant_id)
        ):
            raise ControlRpcError(
                code=4202,
                message="authoritative live runtime unavailable",
            )
        issued_at, expires_at = self._request_window()
        result = await self._broker.route_request(
            control_connection_id=context.connection_id,
            request_id=str(self._request_id_factory()),
            operation=method,
            issued_at=issued_at,
            expires_at=expires_at,
            body=body,
            timeout_seconds=self._timeout_seconds,
        )
        if method != "session.control.status":
            return result
        controller_kind = result.get("controller_kind")
        if controller_kind == "local":
            result = {**result, "controller_kind": "desktop"}
            controller_kind = "desktop"
        controller_label = result.get("controller_label")
        if (
            controller_kind not in {"desktop", "mobile", "none"}
            or (controller_kind == "none" and controller_label is not None)
            or (
                controller_kind != "none"
                and (not isinstance(controller_label, str) or not controller_label)
            )
        ):
            raise ControlRpcError(
                code=4201,
                message="control contract unsupported",
            )
        return result

    async def close(
        self,
        *,
        context: ControlRequestContext,
        reason: str,
    ) -> None:
        issued_at, expires_at = self._request_window()
        await self._broker.close_transport(
            control_connection_id=context.connection_id,
            request_id=str(self._request_id_factory()),
            issued_at=issued_at,
            expires_at=expires_at,
            reason=reason,
            timeout_seconds=self._timeout_seconds,
        )
        self._routes.pop(context.connection_id, None)

    def _request_window(self) -> tuple[str, str]:
        issued_at = self._now()
        if issued_at.tzinfo is None or issued_at.utcoffset() is None:
            raise ValueError("control clock must return an aware datetime")
        issued_at = issued_at.astimezone(UTC)
        expires_at = issued_at + timedelta(seconds=self._timeout_seconds)
        return _utc_text(issued_at), _utc_text(expires_at)

    @staticmethod
    def _verify_ticket_binding(
        context: ControlRequestContext,
        params: Mapping[str, object],
    ) -> None:
        authentication = context.authentication
        expected = {
            "session_id": str(authentication.session_id),
        }
        for field, value in expected.items():
            if field in params and params[field] != value:
                raise ControlRpcError(
                    code=4212,
                    message="session binding mismatch",
                )

    @staticmethod
    def _operation_body(
        method: str,
        params: Mapping[str, object],
    ) -> dict[str, object]:
        required, optional = _parameter_fields(method)
        fields = set(params)
        if not required <= fields <= required | optional:
            _invalid_params()
        session_id = params.get("session_id")
        if (
            not isinstance(session_id, str)
            or not _canonical_uuid(session_id)
        ):
            _invalid_params()
        for field in fields & {
            "lease_id",
            "method",
            "client_request_id",
            "client_turn_id",
            "request_id",
            "choice_id",
        }:
            maximum = 128 if field == "profile" else 256
            if not _canonical_text(params[field], maximum=maximum):
                _invalid_params()
        if "client_instance_id" in params:
            try:
                parsed_client = UUID(str(params["client_instance_id"]))
            except (TypeError, ValueError):
                _invalid_params()
            if str(parsed_client) != params["client_instance_id"]:
                _invalid_params()
        if method == "session.control.acquire":
            return _without_ticket_binding(params)
        if method == "session.command.status":
            if params["method"] not in _OWNER_ACTION_METHODS:
                _invalid_params()
            return _without_ticket_binding(params)
        if method in {
            "session.control.renew",
            "session.control.release",
            "session.control.status",
            "session.interrupt",
        }:
            return _without_ticket_binding(params)
        if method in {"prompt.submit", "session.steer"}:
            if not _nonblank_bounded_text(params["text"]):
                _invalid_params()
            return _without_ticket_binding(params)
        if method == "approval.respond":
            if params["choice"] not in {
                "allow_once",
                "allow_session",
                "allow_always",
                "deny",
            }:
                _invalid_pending_response()
            return _without_ticket_binding(params)
        if method == "clarify.respond":
            answer_fields = {"choice_id", "other_text"} & fields
            if len(answer_fields) != 1:
                _invalid_pending_response()
            if "other_text" in answer_fields and not _nonblank_bounded_text(
                params["other_text"]
            ):
                _invalid_pending_response()
            return _without_ticket_binding(params)
        raise AssertionError(method)


def _parameter_fields(method: str) -> tuple[set[str], set[str]]:
    session = {"session_id"}
    if method == "session.control.acquire":
        return session, set()
    if method in {"session.control.renew", "session.control.release"}:
        return session | {"lease_id"}, set()
    if method == "session.control.status":
        return session, set()
    if method == "session.command.status":
        return (
            session | {"method", "client_request_id"},
            set(),
        )
    mutation = session | {"lease_id", "client_request_id"}
    if method == "prompt.submit":
        return mutation | {"client_turn_id", "text"}, set()
    if method == "session.interrupt":
        return mutation, set()
    if method == "session.steer":
        return mutation | {"text"}, set()
    if method == "approval.respond":
        return mutation | {"request_id", "choice"}, set()
    if method == "clarify.respond":
        return mutation | {"request_id"}, {"choice_id", "other_text"}
    raise AssertionError(method)


def _without_ticket_binding(params: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in params.items()
        if key != "session_id"
    }


def _canonical_uuid(value: str) -> bool:
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.version in {1, 2, 3, 4, 5}


def _canonical_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and value == value.strip()
        and "\x00" not in value
    )


def _nonblank_bounded_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.encode("utf-8")) <= 131_072
    )


def _invalid_params() -> None:
    raise ControlRpcError(code=-32602, message="invalid params")


def _invalid_pending_response() -> None:
    raise ControlRpcError(code=4213, message="invalid pending-input response")


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
