from __future__ import annotations

import asyncio
import json
import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

from hermes_connector.domain.owner_control import (
    OwnerControlCallFailed,
    OwnerControlOutcomeUnknown,
    OwnerControlRequest,
    OwnerControlResponse,
)
from hermes_connector.ports.owner_control import (
    OwnerControlChannelFactoryPort,
    OwnerControlChannelPort,
)


@dataclass(frozen=True, slots=True)
class OwnerControlScope:
    control_transport_id: UUID
    principal_id: str
    client_instance_id: UUID
    session_key: str
    profile: str


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    digest: bytes
    response: OwnerControlResponse


class OwnerControlLane:
    """Ephemeral owner-control RPC lanes bound to live Plugin transports."""

    def __init__(
        self,
        *,
        factory: OwnerControlChannelFactoryPort,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_parallel_transports: int = 8,
        cache_entries_per_transport: int = 128,
    ) -> None:
        if type(max_parallel_transports) is not int or max_parallel_transports <= 0:
            raise ValueError("max_parallel_transports must be positive")
        if (
            type(cache_entries_per_transport) is not int
            or cache_entries_per_transport <= 0
        ):
            raise ValueError("cache_entries_per_transport must be positive")
        self._factory = factory
        self._utc_now = utc_now
        self._parallel = asyncio.Semaphore(max_parallel_transports)
        self._cache_entries = cache_entries_per_transport
        self._channels: dict[UUID, OwnerControlChannelPort] = {}
        self._scopes: dict[UUID, OwnerControlScope] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._cache: dict[UUID, OrderedDict[UUID, _CachedResponse]] = {}
        self._registry_lock = asyncio.Lock()

    async def process(self, request: OwnerControlRequest) -> OwnerControlResponse:
        lock = await self._transport_lock(request.control_transport_id)
        async with lock:
            digest = _request_digest(request)
            cached = self._cached(request.control_transport_id, request.request_id)
            if cached is not None:
                if cached.digest == digest:
                    return cached.response
                return self._failure(
                    request,
                    4207,
                    "request_id_payload_conflict",
                )
            if self._utc_now() >= request.expires_at:
                response = self._failure(
                    request,
                    4306,
                    "deadline_exceeded_before_effect",
                )
                self._remember(request, digest, response)
                return response
            async with self._parallel:
                response = await self._execute(request)
            self._remember(request, digest, response)
            return response

    async def close_all(self) -> None:
        async with self._registry_lock:
            transports = tuple(sorted(self._locks, key=str))
        acquired: list[asyncio.Lock] = []
        try:
            for transport_id in transports:
                lock = self._locks.get(transport_id)
                if lock is not None:
                    await lock.acquire()
                    acquired.append(lock)
            channels = tuple(self._channels.values())
            self._channels.clear()
            self._scopes.clear()
            self._cache.clear()
            for channel in channels:
                try:
                    await channel.close()
                except (ConnectionError, OSError, TimeoutError):
                    continue
        finally:
            for lock in reversed(acquired):
                lock.release()

    async def _execute(
        self,
        request: OwnerControlRequest,
    ) -> OwnerControlResponse:
        try:
            if request.operation == "control.transport.open":
                result = await self._open(request)
            elif request.operation == "control.transport.close":
                result = await self._close(request)
            else:
                channel = self._channels.get(request.control_transport_id)
                if channel is None:
                    raise OwnerControlCallFailed(4200, "control_role_required")
                result = await channel.execute(
                    operation=request.operation,
                    request_id=request.request_id,
                    body=request.body,
                    timeout_seconds=self._remaining(request),
                )
            return OwnerControlResponse(
                request_id=request.request_id,
                control_transport_id=request.control_transport_id,
                operation=request.operation,
                state="succeeded",
                completed_at=self._utc_now(),
                result=MappingProxyType(dict(result)),
            )
        except OwnerControlOutcomeUnknown:
            return OwnerControlResponse(
                request_id=request.request_id,
                control_transport_id=request.control_transport_id,
                operation=request.operation,
                state="unknown",
                completed_at=self._utc_now(),
                error=MappingProxyType({"code": 4307, "reason": "effect_unknown"}),
            )
        except OwnerControlCallFailed as error:
            return self._failure(request, error.code, error.reason)
        except TimeoutError:
            return self._failure(
                request,
                4306,
                "deadline_exceeded_before_effect",
            )
        except (ConnectionError, OSError, TypeError, ValueError):
            return self._failure(request, 4214, "owner_adapter_unavailable")

    async def _open(
        self,
        request: OwnerControlRequest,
    ) -> Mapping[str, object]:
        if request.control_transport_id in self._channels:
            raise OwnerControlCallFailed(4207, "request_id_payload_conflict")
        body = request.body
        scope = OwnerControlScope(
            control_transport_id=request.control_transport_id,
            principal_id=str(body["principal_id"]),
            client_instance_id=UUID(str(body["client_instance_id"])),
            session_key=str(body["session_key"]),
            profile=str(body["profile"]),
        )
        channel = await self._factory.open(
            scope=scope,
            request_id=request.request_id,
            timeout_seconds=self._remaining(request),
        )
        self._channels[request.control_transport_id] = channel
        self._scopes[request.control_transport_id] = scope
        return MappingProxyType({"attached": True, "connection_role": "control"})

    async def _close(
        self,
        request: OwnerControlRequest,
    ) -> Mapping[str, object]:
        channel = self._channels.pop(request.control_transport_id, None)
        self._scopes.pop(request.control_transport_id, None)
        if channel is None:
            raise OwnerControlCallFailed(4200, "control_role_required")
        await channel.close()
        return MappingProxyType({"closed": True})

    async def _transport_lock(self, transport_id: UUID) -> asyncio.Lock:
        async with self._registry_lock:
            return self._locks.setdefault(transport_id, asyncio.Lock())

    def _remaining(self, request: OwnerControlRequest) -> float:
        remaining = (request.expires_at - self._utc_now()).total_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError
        return remaining

    def _cached(
        self,
        transport_id: UUID,
        request_id: UUID,
    ) -> _CachedResponse | None:
        cache = self._cache.get(transport_id)
        return None if cache is None else cache.get(request_id)

    def _remember(
        self,
        request: OwnerControlRequest,
        digest: bytes,
        response: OwnerControlResponse,
    ) -> None:
        cache = self._cache.setdefault(
            request.control_transport_id,
            OrderedDict(),
        )
        cache[request.request_id] = _CachedResponse(digest, response)
        cache.move_to_end(request.request_id)
        while len(cache) > self._cache_entries:
            cache.popitem(last=False)

    def _failure(
        self,
        request: OwnerControlRequest,
        code: int,
        reason: str,
    ) -> OwnerControlResponse:
        return OwnerControlResponse(
            request_id=request.request_id,
            control_transport_id=request.control_transport_id,
            operation=request.operation,
            state="failed",
            completed_at=self._utc_now(),
            error=MappingProxyType({"code": code, "reason": reason}),
        )


def _request_digest(request: OwnerControlRequest) -> bytes:
    return json.dumps(
        {
            "operation": request.operation,
            "body": _plain(request.body),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
