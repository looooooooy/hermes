"""Shared fakes and fixtures for Plugin runtime lifecycle tests."""

from __future__ import annotations

import json


class _RecordingResource:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        on_start=None,
        on_drain=None,
        on_stop=None,
    ) -> None:
        self.name = name
        self._events = events
        self._on_start = on_start
        self._on_drain = on_drain
        self._on_stop = on_stop

    def start(self, deadline: float) -> None:
        self._events.append(f"start:{self.name}")
        if self._on_start is not None:
            self._on_start(deadline)

    def drain(self, deadline: float) -> None:
        self._events.append(f"drain:{self.name}")
        if self._on_drain is not None:
            self._on_drain(deadline)

    def stop(self, deadline: float) -> None:
        self._events.append(f"stop:{self.name}")
        if self._on_stop is not None:
            self._on_stop(deadline)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _InjectedHandshakeAdapter:
    def __init__(self, generation: str) -> None:
        self.generation = generation

    def handle_hello(self, raw) -> str:
        return f"{self.generation}:{raw}"


def _hello() -> str:
    return json.dumps(
        {
            "contract_version": 1,
            "message_type": "local.hello",
            "client_instance_id": "11111111-1111-4111-8111-111111111111",
            "profile": "default",
            "required_capabilities": ["session.observe"],
            "optional_capabilities": ["session.control", "view.card"],
        }
    )
