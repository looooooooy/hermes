"""Reader-friendly E2E evidence returned by the local harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActiveSessionEvidence:
    plugin_ready: bool
    descriptor_trusted: bool
    endpoint_count: int
    connector_state: str
    state_history: tuple[str, ...]
    runtime_generation: str | None
    accepted_capabilities: tuple[str, ...]
    unavailable_optional_capabilities: tuple[str, ...]
    descriptor_removed: bool
    socket_removed: bool
    leaked_async_tasks: tuple[str, ...]
    leaked_threads: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RejectionEvidence:
    error_code: int | None
    error_reason: str | None
    plugin_still_ready: bool
    command_effects: int
    descriptor_removed: bool
    socket_removed: bool
    leaked_async_tasks: tuple[str, ...]
    leaked_threads: tuple[str, ...]
