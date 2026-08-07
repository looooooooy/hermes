from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from uuid import UUID

from hermes_connector.domain.local_gateway import (
    DISCOVERY_DESCRIPTOR_FIELDS,
    DISCOVERY_DESCRIPTOR_VERSION,
    AgentEndpoint,
    ProcessIdentityEvidence,
)

from .named_pipe import (
    connect_same_user_pipe,
    profile_pipe_name,
    read_line,
    write_all,
)
from .process_identity import current_process_identity, normalize_process_identity

_MAX_DESCRIPTOR_BYTES = 16_384
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class WindowsAgentDiscovery:
    """Discover one Host authority through a SID-bound discovery Named Pipe."""

    def __init__(self, *, timeout_seconds: float = 1.5) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    async def discover(self, profile: str) -> tuple[AgentEndpoint, ...]:
        if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
            return ()
        try:
            endpoint = await asyncio.to_thread(self._discover_sync, profile)
        except (OSError, TimeoutError, ValueError, PermissionError, UnicodeError):
            return ()
        return () if endpoint is None else (endpoint,)

    async def aclose(self) -> None:
        return None

    def _discover_sync(self, profile: str) -> AgentEndpoint | None:
        discovery_name = profile_pipe_name("discovery", profile)
        gateway_name = profile_pipe_name("gateway", profile)
        connection = connect_same_user_pipe(
            discovery_name,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            request = json.dumps(
                {
                    "schema_version": 1,
                    "method": "local-gateway.discover",
                    "profile": profile,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            write_all(connection.handle, request)
            raw = read_line(
                connection.handle,
                maximum=_MAX_DESCRIPTOR_BYTES,
                deadline=time.monotonic() + self._timeout_seconds,
            )
        finally:
            connection.close()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != DISCOVERY_DESCRIPTOR_FIELDS:
            return None
        if value.get("version") != DISCOVERY_DESCRIPTOR_VERSION:
            return None
        pid = value.get("pid")
        if type(pid) is not int or pid != connection.server_pid or pid <= 0:
            return None
        if value.get("profile") != profile:
            return None
        runtime_generation = value.get("runtime_generation")
        if (
            not isinstance(runtime_generation, str)
            or not 1 <= len(runtime_generation) <= 128
            or runtime_generation != runtime_generation.strip()
            or "\x00" in runtime_generation
        ):
            return None
        instance_id = value.get("instance_id")
        try:
            if not isinstance(instance_id, str) or str(UUID(instance_id)) != instance_id:
                return None
        except ValueError:
            return None
        host_bundle_id = value.get("host_bundle_id")
        if not isinstance(host_bundle_id, str) or _BUNDLE_ID.fullmatch(host_bundle_id) is None:
            return None
        socket_path = value.get("socket_path")
        if socket_path != gateway_name:
            return None
        process_identity = normalize_process_identity(
            ProcessIdentityEvidence(
                start_time_ns=value.get("process_start_time_ns"),
                executable_path=Path(value.get("process_executable", "")),
                executable_device=value.get("process_executable_device"),
                executable_inode=value.get("process_executable_inode"),
            )
        )
        if process_identity is None or current_process_identity(pid) != process_identity:
            return None
        return AgentEndpoint(
            pid=pid,
            profile=profile,
            socket_path=Path(socket_path),
            instance_id=instance_id,
            runtime_generation=runtime_generation,
            host_bundle_id=host_bundle_id,
            process_identity=process_identity,
            socket_device=0,
            socket_inode=0,
            registry_path=Path(discovery_name),
        )


__all__ = ["WindowsAgentDiscovery"]
