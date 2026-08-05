from __future__ import annotations

from typing import Protocol


class SupervisorConfigPort(Protocol):
    @property
    def start_deadline_seconds(self) -> float:
        """Return the positive Supervisor startup deadline in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``float``. Errors: configuration validation errors.
        """

    @property
    def stop_deadline_seconds(self) -> float:
        """Return the positive Supervisor shutdown deadline in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``float``. Errors: configuration validation errors.
        """


class StorageConfigPort(Protocol):
    @property
    def transport_journal_entries(self) -> int:
        """Return the bounded durable transport journal capacity in frames."""

    @property
    def bounded_queue_items(self) -> int:
        """Return the storage write-queue capacity in item count.

        Input/unit: none/items. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``int``. Errors: configuration validation errors.
        """

    @property
    def storage_write_deadline_seconds(self) -> float:
        """Return the per-write storage deadline in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``float``. Errors: configuration validation errors.
        """

    @property
    def storage_busy_timeout_ms(self) -> int:
        """Return SQLite lock-wait policy in milliseconds.

        Input/unit: none/milliseconds. Deadline: none; configuration read only.
        Idempotency/effect: repeatable and side-effect free.
        Return: non-negative ``int``. Errors: configuration validation errors.
        """


class LocalGatewayConfigPort(Protocol):
    @property
    def local_connect_timeout_seconds(self) -> float:
        """Return one Local Gateway connection timeout in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``float``. Errors: configuration validation errors.
        """

    @property
    def local_rpc_deadline_seconds(self) -> float:
        """Return the end-to-end local RPC deadline in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``float``. Errors: configuration validation errors.
        """

    @property
    def local_max_reconnect_attempts(self) -> int:
        """Return the bounded reconnect-attempt count.

        Input/unit: none/attempts. Deadline: none; configuration read only.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``int``. Errors: configuration validation errors.
        """

    @property
    def local_reconnect_delay_seconds(self) -> float:
        """Return the delay between reconnect attempts in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: non-negative ``float``. Errors: configuration validation errors.
        """

    @property
    def local_discovery_poll_interval_seconds(self) -> float:
        """Return the Agent discovery polling interval in seconds.

        Input/unit: none/seconds. Deadline: none; this is a configuration read.
        Idempotency/effect: repeatable and side-effect free.
        Return: positive ``float``. Errors: configuration validation errors.
        """
