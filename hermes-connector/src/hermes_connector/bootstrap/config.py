from __future__ import annotations

import math
from dataclasses import dataclass, fields

_INTEGER_LIMIT_FIELDS = frozenset(
    {
        "local_max_reconnect_attempts",
        "bounded_queue_items",
        "command_retention_entries",
        "transport_journal_entries",
        "storage_busy_timeout_ms",
    }
)


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    local_connect_timeout_seconds: float = 2.0
    local_rpc_deadline_seconds: float = 3.0
    local_max_reconnect_attempts: int = 3
    local_reconnect_delay_seconds: float = 0.25
    local_discovery_poll_interval_seconds: float = 5.0
    cloud_heartbeat_interval_seconds: float = 20.0
    start_deadline_seconds: float = 10.0
    stop_deadline_seconds: float = 10.0
    bounded_queue_items: int = 256
    command_retention_entries: int = 1_024
    transport_journal_entries: int = 2_048
    storage_write_deadline_seconds: float = 3.0
    storage_busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in _INTEGER_LIMIT_FIELDS:
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{field.name} must be a positive integer")
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{field.name} must be a positive number")
            if value <= 0:
                raise ValueError(f"{field.name} must be positive")
