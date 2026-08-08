"""Supervisor component that publishes the safe activation-health receipt."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from hermes_connector.adapters.status_receipt_codec import (
    normalize_process_identity_evidence,
)
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)
from hermes_connector.domain.readiness_status import (
    ACTIVATION_LOCAL_CAPABILITIES,
    ConnectorStatusReceipt,
    LocalAuthorityIdentity,
    validate_release_id,
)


class ReadySource(Protocol):
    async def ready(self) -> bool: ...


class CloudReadySource(ReadySource, Protocol):
    @property
    def state(self) -> CloudSessionState: ...


class StatusReceiptStore(Protocol):
    @property
    def path(self) -> Path: ...

    def publish(self, receipt: ConnectorStatusReceipt) -> None: ...

    def remove(self) -> None: ...


AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]
ProcessIdentityProvider = Callable[[int], ProcessIdentityEvidence | None]


class ReadinessStatusComponent:
    """Publish only the readiness state proven by existing runtime authorities."""

    name = "readiness_status"

    def __init__(
        self,
        *,
        store: StatusReceiptStore,
        release_id: str,
        local_authority: AuthorityProvider,
        storage: ReadySource,
        directory: ReadySource,
        cloud: CloudReadySource,
        pid: int,
        process_identity_provider: ProcessIdentityProvider,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        refresh_interval_seconds: float = 5.0,
    ) -> None:
        if (
            isinstance(refresh_interval_seconds, bool)
            or not isinstance(refresh_interval_seconds, (int, float))
            or refresh_interval_seconds <= 0
        ):
            raise ValueError("status receipt refresh interval is invalid")
        if type(pid) is not int or not 1 <= pid <= 2_147_483_647:
            raise ValueError("Connector process id is invalid")
        self._store = store
        self._release_id = validate_release_id(release_id)
        self._local_authority = local_authority
        self._storage = storage
        self._directory = directory
        self._cloud = cloud
        self._pid = pid
        self._process_identity_provider = process_identity_provider
        self._now = now
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self._process_identity = None
        self._last_authority: LocalRuntimeAuthority | None = None
        self._activated: asyncio.Event | None = None
        self._stopping: asyncio.Event | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("readiness status component can only be started once")
        process_identity = normalize_process_identity_evidence(
            self._process_identity_provider(self._pid)
        )
        if process_identity is None:
            raise RuntimeError("Connector process identity is unavailable")
        self._process_identity = process_identity
        self._activated = asyncio.Event()
        self._stopping = asyncio.Event()
        self._started = True

    async def ready(self) -> bool:
        self._require_started()
        ready, authority, cloud_state = await self._current_readiness()
        if not ready or authority is None:
            return False
        self._last_authority = authority
        self._publish(ready=True, authority=authority, cloud_state=cloud_state)
        self._require_activated().set()
        return True

    async def run(self) -> None:
        self._require_started()
        activated = self._require_activated()
        stopping = self._require_stopping()
        await activated.wait()
        while not stopping.is_set():
            try:
                await asyncio.wait_for(
                    stopping.wait(),
                    timeout=self._refresh_interval_seconds,
                )
            except TimeoutError:
                pass
            if stopping.is_set():
                return
            ready, authority, cloud_state = await self._current_readiness()
            if authority is not None:
                self._last_authority = authority
            retained = authority or self._last_authority
            if retained is not None:
                self._publish(
                    ready=ready,
                    authority=retained,
                    cloud_state=cloud_state,
                )

    async def drain(self) -> None:
        if not self._started:
            return
        authority = self._last_authority
        if authority is not None and self._store.path.exists():
            self._publish(
                ready=False,
                authority=authority,
                cloud_state=self._cloud.state,
            )
        self._require_stopping().set()
        self._require_activated().set()

    async def stop(self) -> None:
        if not self._started:
            return
        self._require_stopping().set()
        self._require_activated().set()
        self._store.remove()

    async def _current_readiness(
        self,
    ) -> tuple[bool, LocalRuntimeAuthority | None, CloudSessionState]:
        authority = await self._local_authority()
        cloud_state = self._cloud.state
        if authority is None:
            return False, None, cloud_state
        capabilities = frozenset(
            (*authority.required_capabilities, *authority.optional_capabilities)
        )
        dependencies_ready = (
            ACTIVATION_LOCAL_CAPABILITIES <= capabilities
            and await self._storage.ready()
            and await self._directory.ready()
            and await self._cloud.ready()
            and cloud_state is CloudSessionState.ACTIVE
        )
        return dependencies_ready, authority, cloud_state

    def _publish(
        self,
        *,
        ready: bool,
        authority: LocalRuntimeAuthority,
        cloud_state: CloudSessionState,
    ) -> None:
        if self._process_identity is None:
            raise RuntimeError("Connector process identity is unavailable")
        self._store.publish(
            ConnectorStatusReceipt(
                release_id=self._release_id,
                pid=self._pid,
                process_identity=self._process_identity,
                runtime_generation=authority.runtime_generation,
                local_authority_identity=LocalAuthorityIdentity(
                    profile=authority.profile,
                    instance_id=authority.instance_id,
                    host_bundle_id=authority.host_bundle_id,
                ),
                cloud_state=cloud_state,
                updated_at=self._now(),
                ready=ready,
            )
        )

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("readiness status component is not started")

    def _require_activated(self) -> asyncio.Event:
        if self._activated is None:
            raise RuntimeError("readiness status component is not started")
        return self._activated

    def _require_stopping(self) -> asyncio.Event:
        if self._stopping is None:
            raise RuntimeError("readiness status component is not started")
        return self._stopping


__all__ = [
    "CloudReadySource",
    "ProcessIdentityProvider",
    "ReadinessStatusComponent",
    "ReadySource",
    "StatusReceiptStore",
]
