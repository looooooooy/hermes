"""Session authority boundary for remote runtime control.

Remote commands must resolve through runtime-owned session authority instead of
creating or mutating sessions directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionBinding:
    session_id: str
    runtime_generation: str
    profile: str


class SessionAuthority:
    """Runtime-owned session lookup boundary."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionBinding] = {}

    def bind(self, binding: SessionBinding) -> None:
        self._sessions[binding.session_id] = binding

    def resolve(self, session_id: str, runtime_generation: str) -> SessionBinding:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError("session unavailable")
        if session.runtime_generation != runtime_generation:
            raise ValueError("runtime generation mismatch")
        return session
