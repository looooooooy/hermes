from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

from hermes_connector.adapters.cloud.codec import (
    ConnectorProtocolCodec,
    InvalidCloudFrame,
)
from hermes_connector.domain.cloud_protocol import (
    CommandDelivery,
    CommandReceipt,
    CommandResult,
)
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.control_command import (
    LocalControlFailure,
    LocalControlOutcomeUnknown,
)
from hermes_connector.domain.storage import CommandOutboxRecord, CommandRecord
from hermes_connector.ports.control_command import (
    CommandLedgerPort,
    LocalControlRelayPort,
)

_METHOD_TTLS = {
    "prompt.submit": timedelta(minutes=5),
    "session.interrupt": timedelta(seconds=10),
}
_SAFE_ERRORS = {
    "control_role_required": "A control connection is required.",
    "control_contract_unsupported": "The control contract is not supported.",
    "live_runtime_unavailable": "The live runtime is unavailable.",
    "controller_conflict": "Another controller owns the session.",
    "lease_required": "A controller lease is required.",
    "lease_expired": "The controller lease expired.",
    "lease_mismatch": "The controller lease does not match the command scope.",
    "request_id_payload_conflict": "The request ID was reused.",
    "pending_request_conflict": "The pending request is no longer actionable.",
    "method_not_allowed": "The method is not allowed.",
    "command_unknown": "The command outcome is unknown.",
    "revision_conflict": "The control revision changed.",
    "session_binding_mismatch": "The session binding changed.",
    "invalid_pending_response": "The pending response is invalid.",
    "owner_adapter_unavailable": "The owner action adapter is unavailable.",
    "relay_overloaded": "The local control relay is overloaded.",
    "internal_temporary": "A temporary local failure occurred.",
}


class CommandRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CommandScope:
    tenant_id: str
    device_id: str
    connector_instance_id: UUID
    profile: str
    allowed_session_keys: frozenset[str] | None


class CommandLane:
    """Durable command state machine, independent of the unfinished Cloud router."""

    def __init__(
        self,
        *,
        storage: CommandLedgerPort,
        relay: LocalControlRelayPort,
        scope: CommandScope,
        codec: ConnectorProtocolCodec,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._relay = relay
        self._scope = scope
        self._codec = codec
        self._clock = clock or (lambda: datetime.now(UTC))

    async def process(self, envelope: CloudEnvelope) -> CommandRecord:
        command = self._decode_and_authorize(envelope)
        now = self._now()
        encoded_delivery = self._codec.encode_command_delivery(command)
        digest = "sha256:" + hashlib.sha256(encoded_delivery).hexdigest()
        delivery_projection = _delivery_projection(command, digest)
        receipt_payload = self._codec.encode_command_receipt(
            CommandReceipt(
                command_id=command.command_id,
                message_id=envelope.message_id,
                connector_instance_id=command.connector_instance_id,
                client_instance_id=command.client_instance_id,
                session_key=command.session_key,
                profile=command.profile,
                client_request_id=command.client_request_id,
                method=command.method,
                state="delivered",
                stored_at=now,
                revision=command.revision,
            )
        )
        inserted = await self._storage.put_command(
            command_id=str(command.command_id),
            message_id=str(envelope.message_id),
            digest=digest,
            delivery_payload=delivery_projection,
            receipt_payload=receipt_payload,
            expires_at=_instant_text(command.expires_at),
            revision=command.revision,
        )
        if not inserted.inserted:
            return inserted.record
        claimed = await self._storage.claim_command(str(command.command_id))
        if not claimed:
            record = await self._storage.get_command(str(command.command_id))
            if record is None:
                raise RuntimeError("durable command disappeared")
            return record

        result = await self._execute(command)
        encoded = self._codec.encode_command_result(result)
        return await self._storage.complete_command(
            command_id=str(command.command_id),
            state=result.state,
            result_payload=encoded,
            revision=result.revision,
        )

    async def recover_inflight(self, *, limit: int) -> int:
        records = await self._storage.command_records(
            state="executing",
            limit=limit,
        )
        for record in records:
            command = self._codec.decode_command_receipt(record.receipt_payload)
            result = self._failure_result(
                command,
                state="unknown",
                code="command_unknown",
                retryable=False,
            )
            await self._storage.complete_command(
                command_id=record.command_id,
                state="unknown",
                result_payload=self._codec.encode_command_result(result),
                revision=result.revision,
            )
        return len(records)

    async def pending_cloud_messages(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_command_id: str | None = None,
        after_message_type: str | None = None,
    ) -> tuple[CommandOutboxRecord, ...]:
        return await self._storage.pending_command_messages(
            limit=limit,
            after_created_at=after_created_at,
            after_command_id=after_command_id,
            after_message_type=after_message_type,
        )

    async def acknowledge_cloud_message(
        self,
        *,
        command_id: str,
        message_type: str,
    ) -> bool:
        return await self._storage.ack_command_message(
            command_id=command_id,
            message_type=message_type,
        )

    def _decode_and_authorize(self, envelope: CloudEnvelope) -> CommandDelivery:
        if envelope.message_type != "command.deliver":
            raise CommandRejected("protocol_message_type")
        if (
            envelope.tenant_id != self._scope.tenant_id
            or envelope.device_id != self._scope.device_id
        ):
            raise CommandRejected("authorization_denied")
        try:
            command = self._codec.decode_command_delivery_payload(envelope.payload)
        except InvalidCloudFrame as error:
            raise CommandRejected("protocol_invalid_command") from error
        session_is_not_allowed = (
            self._scope.allowed_session_keys is not None
            and command.session_key not in self._scope.allowed_session_keys
        )
        if (
            command.connector_instance_id != self._scope.connector_instance_id
            or command.profile != self._scope.profile
            or session_is_not_allowed
        ):
            raise CommandRejected("session_binding_mismatch")
        now = self._now()
        if command.issued_at > now:
            raise CommandRejected("command_not_yet_valid")
        if command.expires_at <= now:
            raise CommandRejected("command_expired")
        if command.expires_at - command.issued_at > _METHOD_TTLS[command.method]:
            raise CommandRejected("command_ttl_exceeds_method_limit")
        return command

    async def _execute(self, command: CommandDelivery) -> CommandResult:
        try:
            local_result = await self._relay.execute(command)
            if not isinstance(local_result, Mapping):
                return self._failure_result(
                    command,
                    state="unknown",
                    code="command_unknown",
                    retryable=False,
                )
            success = CommandResult(
                **_result_identity(command),
                state="succeeded",
                completed_at=self._now(),
                revision=command.revision + 1,
                result=MappingProxyType(dict(local_result)),
            )
            self._codec.encode_command_result(success)
            return success
        except LocalControlFailure as error:
            code = error.code if error.code in _SAFE_ERRORS else "internal_temporary"
            return self._failure_result(
                command,
                state="failed",
                code=code,
                retryable=error.retryable,
            )
        except (InvalidCloudFrame, LocalControlOutcomeUnknown):
            return self._failure_result(
                command,
                state="unknown",
                code="command_unknown",
                retryable=False,
            )

    def _failure_result(
        self,
        command: CommandDelivery | CommandReceipt,
        *,
        state: str,
        code: str,
        retryable: bool,
    ) -> CommandResult:
        return CommandResult(
            **_result_identity(command),
            state=state,
            completed_at=self._now(),
            revision=command.revision + 1,
            error=MappingProxyType(
                {
                    "code": code,
                    "message": _SAFE_ERRORS[code],
                    "retryable": retryable,
                }
            ),
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise RuntimeError("command clock must return UTC")
        return now.astimezone(UTC)


def _result_identity(
    command: CommandDelivery | CommandReceipt,
) -> dict[str, object]:
    return {
        "command_id": command.command_id,
        "connector_instance_id": command.connector_instance_id,
        "client_instance_id": command.client_instance_id,
        "session_key": command.session_key,
        "profile": command.profile,
        "client_request_id": command.client_request_id,
        "method": command.method,
    }


def _instant_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _delivery_projection(command: CommandDelivery, digest: str) -> bytes:
    return json.dumps(
        {
            "command_id": str(command.command_id),
            "connector_instance_id": str(command.connector_instance_id),
            "client_instance_id": str(command.client_instance_id),
            "session_key": command.session_key,
            "profile": command.profile,
            "client_request_id": command.client_request_id,
            "method": command.method,
            "issued_at": _instant_text(command.issued_at),
            "expires_at": _instant_text(command.expires_at),
            "revision": command.revision,
            "delivery_digest": digest,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
