"""Runtime-owned session authority for remote control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionController(Protocol):
    def interrupt(self) -> None: ...

    def resume(self) -> None: ...

    def approve(self, payload: Mapping[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionBinding:
    session_id: str
    runtime_generation: str
    profile: str
    controller: SessionController

    def __post_init__(self) -> None:
        for name, value in (
            ("session_id", self.session_id),
            ("runtime_generation", self.runtime_generation),
            ("profile", self.profile),
        ):
            if not value or value != value.strip():
                raise ValueError(f"{name} must be canonical non-empty text")
        if not isinstance(self.controller, SessionController):
            raise TypeError("controller does not implement SessionController")


class SessionAuthority:
    """Maps durable session keys to Runtime-owned session controllers."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionBinding] = {}
        self._lock = RLock()

    def bind(self, binding: SessionBinding) -> None:
        with self._lock:
            existing = self._sessions.get(binding.session_id)
            if (
                existing is not None
                and existing.runtime_generation == binding.runtime_generation
                and existing.controller is not binding.controller
            ):
                raise ValueError("session binding conflict")
            self._sessions[binding.session_id] = binding

    def unbind(self, session_id: str, runtime_generation: str | None = None) -> bool:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                return False
            if (
                runtime_generation is not None
                and existing.runtime_generation != runtime_generation
            ):
                return False
            del self._sessions[session_id]
            return True

    def resolve(self, session_id: str, runtime_generation: str) -> SessionBinding:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError("session unavailable")
            if session.runtime_generation != runtime_generation:
                raise ValueError("runtime generation mismatch")
            return session

    def snapshot(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(
                {
                    "session_id": item.session_id,
                    "runtime_generation": item.runtime_generation,
                    "profile": item.profile,
                }
                for item in self._sessions.values()
            )
