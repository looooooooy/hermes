from __future__ import annotations

from typing import Protocol

from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.storage import (
    CommandOutboxRecord,
    CommandPutResult,
    CommandRecord,
)


class CommandLanePort(Protocol):
    async def process(self, envelope: CloudEnvelope) -> object: ...

    async def recover_inflight(self, *, limit: int) -> int: ...

    async def pending_cloud_messages(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_command_id: str | None = None,
        after_message_type: str | None = None,
    ) -> tuple[CommandOutboxRecord, ...]: ...

    async def acknowledge_cloud_message(
        self,
        *,
        command_id: str,
        message_type: str,
    ) -> bool: ...


class LocalControlRelayPort(Protocol):
    async def execute(self, command: CommandDelivery) -> object:
        """Execute one already-authorized command through the owner Plugin."""


class CommandLedgerPort(Protocol):
    async def put_command(
        self,
        *,
        command_id: str,
        message_id: str,
        digest: str,
        delivery_payload: bytes,
        receipt_payload: bytes,
        expires_at: str,
        revision: int,
    ) -> CommandPutResult: ...

    async def get_command(self, command_id: str) -> CommandRecord | None: ...

    async def claim_command(self, command_id: str) -> bool: ...

    async def complete_command(
        self,
        *,
        command_id: str,
        state: str,
        result_payload: bytes,
        revision: int,
    ) -> CommandRecord: ...

    async def command_records(
        self,
        *,
        state: str | None,
        limit: int,
    ) -> tuple[CommandRecord, ...]: ...

    async def pending_command_messages(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_command_id: str | None = None,
        after_message_type: str | None = None,
    ) -> tuple[CommandOutboxRecord, ...]: ...

    async def ack_command_message(
        self,
        *,
        command_id: str,
        message_type: str,
    ) -> bool: ...
