from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    current_process_identity,
    normalize_process_identity,
)
from hermes_connector.contracts.mobile_control import CONTROL_ERROR_REASONS
from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.control_command import (
    LocalControlFailure,
    LocalControlOutcomeUnknown,
)
from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.local_gateway import (
    DISCOVERY_DESCRIPTOR_FIELDS,
    DISCOVERY_DESCRIPTOR_VERSION,
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)
from hermes_connector.domain.owner_control import (
    OwnerControlCallFailed,
    OwnerControlOutcomeUnknown,
)
from hermes_connector.ports.owner_control import OwnerControlScopePort

_MAX_DESCRIPTOR_BYTES = 16_384
_MAX_FRAME_BYTES = 262_144
_MAX_CANDIDATES = 32
_MAX_DIRECTORY_ENTRIES = 64
_MAX_SOCKET_PATH_BYTES = 103
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_DESCRIPTOR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_DESCRIPTOR_VERSION = DISCOVERY_DESCRIPTOR_VERSION
_DESCRIPTOR_FIELDS = DISCOVERY_DESCRIPTOR_FIELDS
_ERROR_NAMES = CONTROL_ERROR_REASONS
_RETRYABLE_CODES = frozenset({4202, 4214, 4215})
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

AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]


@dataclass(frozen=True, slots=True)
class ControlEndpoint:
    pid: int
    profile: str
    socket_path: Path
    instance_id: str
    runtime_generation: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidence
    socket_device: int
    socket_inode: int


class MacOSPluginControlRelay:
    """Invoke the canonical Plugin control relay over one trusted UDS connection."""

    def __init__(
        self,
        *,
        registry_directory: Path,
        socket_directory: Path,
        profile: str,
        user_id: str,
        provider: str,
        authority: AuthorityProvider,
        timeout_seconds: float = 3.0,
        process_identity_provider: ProcessIdentityProvider | None = None,
    ) -> None:
        if not registry_directory.is_absolute() or not socket_directory.is_absolute():
            raise ValueError("control relay directories must be absolute")
        if _PROFILE.fullmatch(profile) is None:
            raise ValueError("profile is invalid")
        if not user_id.strip() or user_id != user_id.strip():
            raise ValueError("user_id is invalid")
        if not provider.strip() or provider != provider.strip():
            raise ValueError("provider is invalid")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._registry_directory = registry_directory
        self._socket_directory = socket_directory
        self._profile = profile
        self._user_id = user_id
        self._provider = provider
        self._authority = authority
        self._timeout_seconds = timeout_seconds
        self._process_identity_provider = (
            process_identity_provider or current_process_identity
        )

    async def execute(self, command: CommandDelivery) -> Mapping[str, object]:
        if command.profile != self._profile:
            raise LocalControlFailure("session_binding_mismatch")
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout_at(deadline):
                    authority = await _require_control_authority(self._authority)
                    endpoints = await asyncio.to_thread(self._discover)
                    if len(endpoints) != 1:
                        raise LocalControlFailure(
                            "owner_adapter_unavailable",
                            retryable=not endpoints,
                        )
                    endpoint = endpoints[0]
                    if not _endpoint_matches_authority(endpoint, authority):
                        raise LocalControlFailure(
                            "owner_adapter_unavailable",
                            retryable=True,
                        )
                    self._require_endpoint_evidence(endpoint)
                    websocket = await stack.enter_async_context(
                        unix_connect(
                            path=str(endpoint.socket_path),
                            uri="ws://localhost/control",
                            max_size=_MAX_FRAME_BYTES,
                        )
                    )
                    if _connected_peer_pid(websocket) != endpoint.pid:
                        raise LocalControlFailure(
                            "owner_adapter_unavailable",
                            retryable=True,
                        )
                    self._require_endpoint_evidence(endpoint)
                    await _require_control_authority(
                        self._authority,
                        expected=authority,
                    )
                    await self._attach(websocket, command)
                    lease_id = await self._acquire(websocket, command)
                return await self._mutate(
                    websocket,
                    command,
                    lease_id,
                    deadline=deadline,
                )
        except LocalControlFailure:
            raise
        except LocalControlOutcomeUnknown:
            raise
        except (OSError, TimeoutError, TypeError, ValueError, WebSocketException):
            raise LocalControlFailure(
                "owner_adapter_unavailable",
                retryable=True,
            ) from None

    def _require_endpoint_evidence(self, endpoint: ControlEndpoint) -> None:
        if not _same_socket_identity(endpoint):
            raise LocalControlFailure(
                "owner_adapter_unavailable",
                retryable=True,
            )
        if not self._process_matches(endpoint.pid, endpoint.process_identity):
            raise LocalControlFailure(
                "owner_adapter_unavailable",
                retryable=True,
            )

    async def _attach(self, websocket: Any, command: CommandDelivery) -> None:
        result = await self._rpc(
            websocket,
            request_id=f"{command.command_id}:attach",
            method="relay.control.attach",
            params={
                "claims": {
                    "user_id": self._user_id,
                    "provider": self._provider,
                    "connection_role": "control",
                    "client_instance_id": str(command.client_instance_id),
                    "session_key": command.session_key,
                    "profile": command.profile,
                }
            },
            outcome_unknown=False,
        )
        if result != {"attached": True, "connection_role": "control"}:
            raise LocalControlFailure("control_role_required")

    async def _acquire(self, websocket: Any, command: CommandDelivery) -> str:
        result = await self._rpc(
            websocket,
            request_id=f"{command.command_id}:acquire",
            method="session.control.acquire",
            params={
                "session_key": command.session_key,
                "runtime_session_id": command.params["runtime_session_id"],
                "runtime_generation": command.params["runtime_generation"],
            },
            outcome_unknown=False,
        )
        lease_id = result.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id:
            raise LocalControlFailure("lease_required")
        return lease_id

    async def _mutate(
        self,
        websocket: Any,
        command: CommandDelivery,
        lease_id: str,
        *,
        deadline: float,
    ) -> Mapping[str, object]:
        params = {
            **dict(command.params),
            "session_key": command.session_key,
            "lease_id": lease_id,
            "client_request_id": command.client_request_id,
        }
        result = await self._rpc(
            websocket,
            request_id=str(command.command_id),
            method=command.method,
            params=params,
            outcome_unknown=True,
            deadline=deadline,
        )
        return MappingProxyType(result)

    async def _rpc(
        self,
        websocket: Any,
        *,
        request_id: str,
        method: str,
        params: Mapping[str, object],
        outcome_unknown: bool,
        deadline: float | None = None,
    ) -> dict[str, object]:
        request = _encode(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        effect_may_have_occurred = outcome_unknown

        async def exchange() -> dict[str, object]:
            await websocket.send(request)
            raw = await websocket.recv()
            return _decode(raw)

        try:
            if deadline is None:
                response = await exchange()
            else:
                async with asyncio.timeout_at(deadline):
                    response = await exchange()
        except (OSError, TimeoutError, TypeError, ValueError, WebSocketException):
            if effect_may_have_occurred:
                raise LocalControlOutcomeUnknown() from None
            raise LocalControlFailure(
                "owner_adapter_unavailable",
                retryable=True,
            ) from None
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            if outcome_unknown:
                raise LocalControlOutcomeUnknown()
            raise LocalControlFailure("control_contract_unsupported")
        fields = set(response)
        if "error" in response:
            if fields != {"jsonrpc", "id", "error"}:
                if outcome_unknown:
                    raise LocalControlOutcomeUnknown()
                raise LocalControlFailure("control_contract_unsupported")
            if outcome_unknown and not _trusted_local_error(response["error"]):
                raise LocalControlOutcomeUnknown()
            raise _local_error(response["error"])
        if fields != {"jsonrpc", "id", "result"}:
            if outcome_unknown:
                raise LocalControlOutcomeUnknown()
            raise LocalControlFailure("control_contract_unsupported")
        result = response["result"]
        if not isinstance(result, dict) or len(result) > 32:
            if outcome_unknown:
                raise LocalControlOutcomeUnknown()
            raise LocalControlFailure("control_contract_unsupported")
        return result

    def _discover(self) -> tuple[ControlEndpoint, ...]:
        registry_fd = _open_trusted_directory(self._registry_directory)
        if registry_fd is None:
            return ()
        socket_fd = _open_trusted_directory(self._socket_directory)
        if socket_fd is None:
            os.close(registry_fd)
            return ()
        try:
            names = _bounded_descriptor_names(registry_fd)
            if names is None:
                return ()
            endpoints: list[ControlEndpoint] = []
            for name in names:
                endpoint = self._read_descriptor(registry_fd, socket_fd, name)
                if endpoint is not None:
                    endpoints.append(endpoint)
            return tuple(endpoints)
        finally:
            os.close(socket_fd)
            os.close(registry_fd)

    def _read_descriptor(
        self,
        registry_fd: int,
        socket_fd: int,
        name: str,
    ) -> ControlEndpoint | None:
        try:
            before = os.stat(name, dir_fd=registry_fd, follow_symlinks=False)
            if not _trusted_metadata(before, mode=0o600, kind=stat.S_ISREG):
                return None
            descriptor_fd = os.open(name, _DESCRIPTOR_FLAGS, dir_fd=registry_fd)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor_fd)
            if (
                not _same_file(before, opened)
                or not _trusted_metadata(opened, mode=0o600, kind=stat.S_ISREG)
                or not 1 <= opened.st_size <= _MAX_DESCRIPTOR_BYTES
            ):
                return None
            raw = _read_bounded_descriptor(descriptor_fd)
            after = os.fstat(descriptor_fd)
            if (
                not _stable_descriptor(opened, after)
                or not _trusted_metadata(after, mode=0o600, kind=stat.S_ISREG)
                or len(raw) != opened.st_size
            ):
                return None
            value = _decode(raw)
        except (OSError, TypeError, UnicodeError, ValueError):
            return None
        finally:
            os.close(descriptor_fd)
        if set(value) != _DESCRIPTOR_FIELDS:
            return None
        if value["version"] != _DESCRIPTOR_VERSION or type(value["version"]) is not int:
            return None
        pid = value["pid"]
        if type(pid) is not int or pid <= 0 or not _pid_is_alive(pid):
            return None
        if value["profile"] != self._profile:
            return None
        instance_id = value["instance_id"]
        if type(instance_id) is not str:
            return None
        try:
            canonical_uuid(instance_id)
        except (TypeError, ValueError):
            return None
        runtime_generation = value["runtime_generation"]
        if (
            not isinstance(runtime_generation, str)
            or not 1 <= len(runtime_generation) <= 128
            or runtime_generation != runtime_generation.strip()
            or "\x00" in runtime_generation
        ):
            return None
        process_identity = _descriptor_process_identity(value)
        host_bundle_id = value["host_bundle_id"]
        if (
            process_identity is None
            or not isinstance(host_bundle_id, str)
            or _BUNDLE_ID.fullmatch(host_bundle_id) is None
            or not self._process_matches(pid, process_identity)
        ):
            return None
        raw_path = value["socket_path"]
        if not isinstance(raw_path, str) or "\x00" in raw_path:
            return None
        socket_path = Path(raw_path)
        if (
            not socket_path.is_absolute()
            or ".." in socket_path.parts
            or socket_path.parent != self._socket_directory
            or len(os.fsencode(socket_path)) > _MAX_SOCKET_PATH_BYTES
        ):
            return None
        socket_metadata = _trusted_socket(socket_fd, socket_path.name)
        if socket_metadata is None or not self._process_matches(pid, process_identity):
            return None
        return ControlEndpoint(
            pid=pid,
            profile=self._profile,
            socket_path=socket_path,
            instance_id=instance_id,
            runtime_generation=runtime_generation,
            host_bundle_id=host_bundle_id,
            process_identity=process_identity,
            socket_device=socket_metadata.st_dev,
            socket_inode=socket_metadata.st_ino,
        )

    def _process_matches(
        self,
        pid: int,
        expected: ProcessIdentityEvidence,
    ) -> bool:
        try:
            observed = self._process_identity_provider(pid)
        except BaseException:  # noqa: BLE001 - process evidence boundary
            return False
        return normalize_process_identity(observed) == expected


class MacOSPluginOwnerControlChannelFactory:
    """Open one persistent Plugin UDS channel for one Cloud control transport."""

    def __init__(
        self,
        *,
        registry_directory: Path,
        socket_directory: Path,
        profile: str,
        provider: str,
        authority: AuthorityProvider,
        process_identity_provider: ProcessIdentityProvider | None = None,
    ) -> None:
        if not registry_directory.is_absolute() or not socket_directory.is_absolute():
            raise ValueError("control relay directories must be absolute")
        if _PROFILE.fullmatch(profile) is None:
            raise ValueError("profile is invalid")
        if not provider.strip() or provider != provider.strip():
            raise ValueError("provider is invalid")
        self._registry_directory = registry_directory
        self._socket_directory = socket_directory
        self._profile = profile
        self._provider = provider
        self._authority = authority
        self._discovery = MacOSPluginControlRelay(
            registry_directory=registry_directory,
            socket_directory=socket_directory,
            profile=profile,
            user_id="owner-control-discovery",
            provider=provider,
            authority=authority,
            process_identity_provider=process_identity_provider,
        )

    async def open(
        self,
        *,
        scope: OwnerControlScopePort,
        request_id: UUID,
        timeout_seconds: float,
    ) -> MacOSPluginOwnerControlChannel:
        if scope.profile != self._profile:
            raise OwnerControlCallFailed(4212, "session_binding_mismatch")
        websocket = None
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                authority = await _require_owner_authority(self._authority)
                endpoints = await asyncio.to_thread(self._discover)
                if len(endpoints) != 1:
                    raise OwnerControlCallFailed(4214, "owner_adapter_unavailable")
                endpoint = endpoints[0]
                if not _endpoint_matches_authority(endpoint, authority):
                    raise OwnerControlCallFailed(4214, "owner_adapter_unavailable")
                self._require_endpoint_evidence(endpoint)
                websocket = await unix_connect(
                    path=str(endpoint.socket_path),
                    uri="ws://localhost/control",
                    max_size=_MAX_FRAME_BYTES,
                )
                if _connected_peer_pid(websocket) != endpoint.pid:
                    raise OwnerControlCallFailed(
                        4214,
                        "owner_adapter_unavailable",
                    )
                self._require_endpoint_evidence(endpoint)
                await _require_owner_authority(
                    self._authority,
                    expected=authority,
                )
                channel = MacOSPluginOwnerControlChannel(
                    websocket=websocket,
                    scope=scope,
                    provider=self._provider,
                    authority=self._authority,
                    expected_authority=authority,
                )
                await channel.attach(
                    request_id=request_id,
                    timeout_seconds=timeout_seconds,
                )
                return channel
        except OwnerControlCallFailed:
            if websocket is not None:
                await _close_owner_websocket(websocket)
            raise
        except TimeoutError:
            if websocket is not None:
                await _close_owner_websocket(websocket)
            deadline_exhausted = asyncio.get_running_loop().time() >= deadline
            raise OwnerControlCallFailed(
                4306 if deadline_exhausted else 4214,
                (
                    "deadline_exceeded_before_effect"
                    if deadline_exhausted
                    else "owner_adapter_unavailable"
                ),
            ) from None
        except (OSError, TypeError, ValueError, WebSocketException):
            if websocket is not None:
                await _close_owner_websocket(websocket)
            raise OwnerControlCallFailed(
                4214,
                "owner_adapter_unavailable",
            ) from None

    def _require_endpoint_evidence(self, endpoint: ControlEndpoint) -> None:
        if not _same_socket_identity(endpoint) or not self._discovery._process_matches(
            endpoint.pid,
            endpoint.process_identity,
        ):
            raise OwnerControlCallFailed(4214, "owner_adapter_unavailable")

    def _discover(self) -> tuple[ControlEndpoint, ...]:
        return self._discovery._discover()


class MacOSPluginOwnerControlChannel:
    """One attached Plugin UDS socket bound to immutable Cloud control scope."""

    def __init__(
        self,
        *,
        websocket: Any,
        scope: OwnerControlScopePort,
        provider: str,
        authority: AuthorityProvider,
        expected_authority: LocalRuntimeAuthority,
    ) -> None:
        self._websocket = websocket
        self._scope = scope
        self._provider = provider
        self._authority = authority
        self._expected_authority = expected_authority
        self._rpc_lock = asyncio.Lock()
        self._closed = False
        self._runtime_session_id: str | None = None

    async def attach(
        self,
        *,
        request_id: UUID,
        timeout_seconds: float,
    ) -> None:
        result = await self._rpc(
            request_id=request_id,
            method="relay.control.attach",
            params={
                "claims": {
                    "user_id": self._scope.principal_id,
                    "provider": self._provider,
                    "connection_role": "control",
                    "client_instance_id": str(self._scope.client_instance_id),
                    "session_key": self._scope.session_key,
                    "profile": self._scope.profile,
                }
            },
            timeout_seconds=timeout_seconds,
            effect_unknown=False,
        )
        if result != {"attached": True, "connection_role": "control"}:
            raise OwnerControlCallFailed(4200, "control_role_required")

    async def execute(
        self,
        *,
        operation: str,
        request_id: UUID,
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> Mapping[str, object]:
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
        if (
            operation != "session.control.acquire"
            and self._runtime_session_id is not None
        ):
            params.setdefault("runtime_session_id", self._runtime_session_id)
        result = await self._rpc(
            request_id=request_id,
            method=operation,
            params=params,
            timeout_seconds=timeout_seconds,
            effect_unknown=True,
        )
        if operation in _OWNER_ACTION_OPERATIONS and result.get("status") == "unknown":
            raise OwnerControlOutcomeUnknown()
        if (
            operation == "session.control.status"
            and result.get("controller_kind") == "local"
        ):
            result = {**result, "controller_kind": "desktop"}
        if operation == "session.control.acquire":
            runtime_session_id = body.get("runtime_session_id")
            if isinstance(runtime_session_id, str):
                self._runtime_session_id = runtime_session_id
        return MappingProxyType(result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_owner_websocket(self._websocket)

    async def _rpc(
        self,
        *,
        request_id: UUID,
        method: str,
        params: Mapping[str, object],
        timeout_seconds: float,
        effect_unknown: bool,
    ) -> dict[str, object]:
        request = _encode(
            {
                "jsonrpc": "2.0",
                "id": str(request_id),
                "method": method,
                "params": dict(params),
            }
        )
        sent = False
        async with self._rpc_lock:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await self._websocket.send(request)
                    sent = True
                    response = _decode(await self._websocket.recv())
            except (
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
                WebSocketException,
            ):
                if sent and effect_unknown:
                    raise OwnerControlOutcomeUnknown() from None
                raise OwnerControlCallFailed(
                    4214,
                    "owner_adapter_unavailable",
                ) from None
        if response.get("jsonrpc") != "2.0" or response.get("id") != str(request_id):
            if effect_unknown:
                raise OwnerControlOutcomeUnknown()
            raise OwnerControlCallFailed(4201, "control_contract_unsupported")
        fields = set(response)
        if "error" in response:
            if fields != {"jsonrpc", "id", "error"}:
                raise OwnerControlCallFailed(4201, "control_contract_unsupported")
            raise _owner_control_error(response["error"])
        if fields != {"jsonrpc", "id", "result"}:
            if effect_unknown:
                raise OwnerControlOutcomeUnknown()
            raise OwnerControlCallFailed(4201, "control_contract_unsupported")
        result = response["result"]
        if not isinstance(result, dict) or len(result) > 32:
            if effect_unknown:
                raise OwnerControlOutcomeUnknown()
            raise OwnerControlCallFailed(4201, "control_contract_unsupported")
        return result


async def _close_owner_websocket(websocket: Any) -> None:
    try:
        await websocket.close()
    except (OSError, TimeoutError, WebSocketException):
        return


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


def _endpoint_matches_authority(
    endpoint: ControlEndpoint,
    authority: LocalRuntimeAuthority,
) -> bool:
    return (
        authority.profile == endpoint.profile
        and endpoint.runtime_generation == authority.runtime_generation
        and endpoint.instance_id == authority.instance_id
        and endpoint.host_bundle_id == authority.host_bundle_id
        and endpoint.process_identity == authority.process_identity
    )


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


def _same_socket_identity(endpoint: ControlEndpoint) -> bool:
    try:
        metadata = endpoint.socket_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.geteuid()
        and metadata.st_dev == endpoint.socket_device
        and metadata.st_ino == endpoint.socket_inode
    )


def _owner_control_error(value: object) -> OwnerControlCallFailed:
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        return OwnerControlCallFailed(4201, "control_contract_unsupported")
    code = value["code"]
    if type(code) is not int or code not in _ERROR_NAMES:
        return OwnerControlCallFailed(4214, "owner_adapter_unavailable")
    return OwnerControlCallFailed(code, _ERROR_NAMES[code])


def _open_trusted_directory(path: Path) -> int | None:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not _trusted_metadata(metadata, mode=0o700, kind=stat.S_ISDIR):
            os.close(descriptor)
            return None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _bounded_descriptor_names(registry_fd: int) -> tuple[str, ...] | None:
    names: list[str] = []
    scanned = 0
    try:
        with os.scandir(registry_fd) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_DIRECTORY_ENTRIES:
                    return None
                if entry.name.startswith(".") or not entry.name.endswith(".json"):
                    continue
                names.append(entry.name)
                if len(names) > _MAX_CANDIDATES:
                    return None
    except OSError:
        return None
    names.sort()
    return tuple(names)


def _read_bounded_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_DESCRIPTOR_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if not 1 <= len(value) <= _MAX_DESCRIPTOR_BYTES:
        raise ValueError("control descriptor size is outside limits")
    return value


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _stable_descriptor(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        _same_file(before, after)
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_size == after.st_size
    )


def _trusted_socket(socket_fd: int, name: str) -> os.stat_result | None:
    if not name or "/" in name or name in {".", ".."}:
        return None
    try:
        metadata = os.stat(name, dir_fd=socket_fd, follow_symlinks=False)
    except OSError:
        return None
    if not _trusted_metadata(metadata, mode=0o600, kind=stat.S_ISSOCK):
        return None
    return metadata


def _descriptor_process_identity(
    value: Mapping[str, object],
) -> ProcessIdentityEvidence | None:
    executable = value["process_executable"]
    if (
        not isinstance(executable, str)
        or not 2 <= len(executable) <= 4096
        or "\x00" in executable
    ):
        return None
    return normalize_process_identity(
        ProcessIdentityEvidence(
            start_time_ns=value["process_start_time_ns"],
            executable_path=Path(executable),
            executable_device=value["process_executable_device"],
            executable_inode=value["process_executable_inode"],
        )
    )


def _trusted_metadata(
    metadata: os.stat_result,
    *,
    mode: int,
    kind: Any,
) -> bool:
    return (
        kind(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == mode
        and metadata.st_uid == os.geteuid()
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _connected_peer_pid(websocket: Any) -> int:
    transport = getattr(websocket, "transport", None)
    if transport is None:
        raise ValueError("control peer identity is unavailable")
    try:
        connected_socket = transport.get_extra_info("socket")
        peer_pid = connected_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
    except (AttributeError, OSError):
        raise ValueError("control peer identity is unavailable") from None
    if type(peer_pid) is not int or peer_pid <= 0:
        raise ValueError("control peer identity is invalid")
    return peer_pid


def _encode(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise LocalControlFailure("control_contract_unsupported") from None
    if len(encoded.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise LocalControlFailure("control_contract_unsupported")
    return encoded


def _decode(raw: object) -> dict[str, object]:
    if isinstance(raw, bytes):
        if len(raw) > _MAX_FRAME_BYTES:
            raise ValueError("local frame is too large")
        text = raw.decode("utf-8", errors="strict")
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
            raise ValueError("local frame is too large")
        text = raw
    else:
        raise TypeError("local frame is not text")
    value = json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_number,
    )
    if not isinstance(value, dict):
        raise TypeError("local frame must be an object")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate local frame field")
        value[key] = item
    return value


def _invalid_number(_value: str) -> None:
    raise ValueError("non-JSON number")


def _local_error(value: object) -> LocalControlFailure:
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        return LocalControlFailure("control_contract_unsupported")
    code = value["code"]
    if type(code) is not int or code not in _ERROR_NAMES:
        return LocalControlFailure("internal_temporary", retryable=False)
    return LocalControlFailure(
        _ERROR_NAMES[code],
        retryable=code in _RETRYABLE_CODES,
    )


def _trusted_local_error(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"code", "message"}
        and type(value["code"]) is int
        and value["code"] in _ERROR_NAMES
        and type(value["message"]) is str
    )
