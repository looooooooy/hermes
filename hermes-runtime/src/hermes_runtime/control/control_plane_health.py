"""Runtime control plane health snapshot.

Provides a single diagnostic view for the remote control execution chain.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ControlPlaneHealth:
    runtime_generation: str
    event_queue_ready: bool
    session_authority_ready: bool
    agent_loop_ready: bool
    receipt_sync_ready: bool

    @property
    def ready(self) -> bool:
        return all(
            (
                self.event_queue_ready,
                self.session_authority_ready,
                self.agent_loop_ready,
                self.receipt_sync_ready,
            )
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "runtime_generation": self.runtime_generation,
            "event_queue_ready": self.event_queue_ready,
            "session_authority_ready": self.session_authority_ready,
            "agent_loop_ready": self.agent_loop_ready,
            "receipt_sync_ready": self.receipt_sync_ready,
            "ready": self.ready,
        }
