"""Bridge between connector commands and runtime event contracts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeEventEnvelope:
    event_id: str
    runtime_generation: str
    session_id: str | None
    event_type: str
    payload: dict[str, object]


class RuntimeEventBridge:
    """Boundary object. Connector does not execute agent logic."""

    def create_event(self, envelope: RuntimeEventEnvelope) -> RuntimeEventEnvelope:
        return envelope
