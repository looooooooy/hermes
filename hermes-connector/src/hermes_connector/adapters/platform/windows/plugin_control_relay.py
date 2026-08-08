"""CommandLane and OwnerControl adapters over the authenticated Windows Control Pipe."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from uuid import UUID

from hermes_connector.adapters.control_response import (
    local_control_result,
    owner_control_result,
)
from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.control_command import (
    LocalControlFailure,
    LocalControlOutcomeUnknown,
)
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.owner_control import (
    OwnerControlCallFailed,
    OwnerControlOutcomeUnknown,
)
from hermes_connector.ports.owner_control import OwnerControlScopePort

from .control_client import WindowsControlRelayClient

AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]
_OWNER_CONTROL_OPERATIONS = frozenset(
    {
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
    }
)
_OWNER_ACTION_OPERATIONS = frozenset(
    {
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    }
)


class WindowsPluginControlRelay:
    """Execute one authorized Cloud command through one exact Host authority."""

    def __init__(
        self,
        *,
        profile: str,
        user_id: str,
        provider: str,
        authority: AuthorityProvider,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(profile, str) or not profile or profile != profile.strip():
            raise ValueError("profile is invalid")
        if not isinstance(user_id, str) or not user_id or user_id != user_id.strip():
            raise ValueError("user_id is invalid")
        if not isinstance(provider, str) or not provider or provider != provider.strip():
            raise ValueError("provider is invalid")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._profile = profile
        self._user_id = user_id
        self._provider = provider
        self._authority = authority
        self._timeout_seconds = float(timeout_seconds)

    async def execute(self, command: CommandDelivery) -> Mapping[str, object]:
        if command.profile != self._profile:
            raise LocalControlFailure("session_binding_mismatch")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        client: WindowsControlRelayClient | None = None
        try:
            authority = await _require_control_authority(self._authority)
            client = WindowsControlRelayClient(
                authority,
                user_id=self._user_id,
                provider=self._provider,
                client_instance_id=command.client_instance_id,
                session_key=command.session_key,
                connect_timeout_seconds=_remaining(loop, deadline),
                io_timeout_seconds=_remaining(loop, deadline),
            )
            await client.open(timeout_seconds=_remaining(loop, deadline))
            await _require_control_authority(self._authority, expected=authority)
            acquire_response = await client.request(
                "session.control.acquire",
                {
                    "session_key": command.session_key,
                    "runtime_session_id": command.params["runtime_session_id"],
                    "runtime_generation": command.params["runtime_generation"],
                },
                timeout_seconds=_remaining(loop, deadline),
            )
            acquire = local_control_result(acquire_response, effect_unknown=False)
            lease_id = acquire.get("lease_id")
            if not isinstance(lease_id, str) or not lease_id:
                raise LocalControlFailure("lease_required")
            await _require_control_authority(self._authority, expected=authority)
            params = {
                **dict(command.params),
                "session_key": command.session_key,
                "lease_id": lease_id,
                "client_request_id": command.client_request_id,
            }
            try:
                response = await client.request(
                    command.method,
                    params,
                    timeout_seconds=_remaining(loop, deadline),
                )
            except (OSError, TimeoutError, TypeError, ValueError, RuntimeError):
                raise LocalControlOutcomeUnknown() from None
            return local_control_result(response, effect_unknown=True)
        except (LocalControlFailure, LocalControlOutcomeUnknown):
            raise
        except (OSError, TimeoutError, TypeError, ValueError, RuntimeError, PermissionError):
            raise LocalControlFailure(
                "owner_adapter_unavailable",
                retryable=True,
            ) from None
        finally:
            if client is not None:
                await client.close()


class WindowsPluginOwnerControlChannelFactory:
    """Open one persistent authority-bound Windows Control Pipe channel."""

    def __init__(
        self,
        *,
        profile: str,
        provider: str,
        authority: AuthorityProvider,
    ) -> None:
        if not isinstance(profile, str) or not profile or profile != profile.strip():
            raise ValueError("profile is invalid")
        if not isinstance(provider, str) or not provider or provider != provider.strip():
            raise ValueError("provider is invalid")
        self._profile = profile
        self._provider = provider
        self._authority = authority

    async def open(
        self,
        *,
        scope: OwnerControlScopePort,
        request_id: UUID,
        timeout_seconds: float,
    ) -> WindowsPluginOwnerControlChannel:
        del request_id
        if scope.profile != self._profile:
            raise OwnerControlCallFailed(4212, "session_binding_mismatch")
        if timeout_seconds <= 0:
            raise OwnerControlCallFailed(4306, "deadline_exceeded_before_effect")
        client: WindowsControlRelayClient | None = None
        try:
            authority = await _require_owner_authority(self._authority)
            client = WindowsControlRelayClient(
                authority,
                user_id=scope.principal_id,
                provider=self._provider,
                client_instance_id=scope.client_instance_id,
                session_key=scope.session_key,
                connect_timeout_seconds=timeout_seconds,
                io_timeout_seconds=timeout_seconds,
            )
            await client.open(timeout_seconds=timeout_seconds)
            await _require_owner_authority(self._authority, expected=authority)
            return WindowsPluginOwnerControlChannel(
                client=client,
                scope=scope,
                authority=self._authority,
                expected_authority=authority,
            )
        except OwnerControlCallFailed:
            if client is not None:
                await client.close()
            raise
        except TimeoutError:
            if client is not None:
                await client.close()
            raise OwnerControlCallFailed(
                4306,
                "deadline_exceeded_before_effect",
            ) from None
        except (OSError, TypeError, ValueError, RuntimeError, PermissionError):
            if client is not None:
                await client.close()
            raise OwnerControlCallFailed(4214, "owner_adapter_unavailable") from None


class WindowsPluginOwnerControlChannel:
    """One attached Windows Control Pipe bound to immutable Cloud control scope."""

    def __init__(
        self,
        *,
        client: WindowsControlRelayClient,
        scope: OwnerControlScopePort,
        authority: AuthorityProvider,
        expected_authority: LocalRuntimeAuthority,
    ) -> None:
        self._client = client
        self._scope = scope
        self._authority = authority
        self._expected_authority = expected_authority
        self._closed = False
        self._runtime_session_id: str | None = None

    async def execute(
        self,
        *,
        operation: str,
        request_id: UUID,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        del request_id
        if operation not in _OWNER_CONTROL_OPERATIONS:
            raise OwnerControlCallFailed(4209, "method_not_allowed")
        if self._closed:
            raise OwnerControlCallFailed(4214, "owner_adapter_unavailable")
        await _require_owner_authority(
            self._authority,
            expected=self._expected_authority,
        )
        params = {
            **dict(body),
            "session_key": self._scope.session_key,
            "profile": self._scope.profile,
            "runtime_generation": self._expected_authority.runtime_generation,
        }
        if operation != "session.control.acquire" and self._runtime_session_id is not None:
            params.setdefault("runtime_session_id", self._runtime_session_id)
        try:
            response = await self._client.request(
                operation,
                params,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, TimeoutError, TypeError, ValueError, RuntimeError):
            raise OwnerControlOutcomeUnknown() from None
        result = dict(owner_control_result(response, effect_unknown=True))
        if operation in _OWNER_ACTION_OPERATIONS and result.get("status") == "unknown":
            raise OwnerControlOutcomeUnknown()
        if operation == "session.control.status" and result.get("controller_kind") == "local":
            result["controller_kind"] = "desktop"
        if operation == "session.control.acquire":
            runtime_session_id = body.get("runtime_session_id")
            if isinstance(runtime_session_id, str):
                self._runtime_session_id = runtime_session_id
        return MappingProxyType(result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()


async def _require_control_authority(
    provider: AuthorityProvider,
    *,
    expected: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    authority = await provider()
    if authority is None or "session.control" not in {
        *authority.required_capabilities,
        *authority.optional_capabilities,
    }:
        raise LocalControlFailure("owner_adapter_unavailable", retryable=True)
    if expected is not None and not _same_authority(authority, expected):
        raise LocalControlFailure("owner_adapter_unavailable", retryable=True)
    return authority


async def _require_owner_authority(
    provider: AuthorityProvider,
    *,
    expected: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    try:
        return await _require_control_authority(provider, expected=expected)
    except LocalControlFailure:
        raise OwnerControlCallFailed(4214, "owner_adapter_unavailable") from None


def _same_authority(
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


def _remaining(loop: asyncio.AbstractEventLoop, deadline: float) -> float:
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError("local control deadline exceeded")
    return remaining


__all__ = [
    "WindowsPluginControlRelay",
    "WindowsPluginOwnerControlChannel",
    "WindowsPluginOwnerControlChannelFactory",
]
