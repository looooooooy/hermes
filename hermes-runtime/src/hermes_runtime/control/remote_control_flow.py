"""Idempotent Runtime-owned remote-control vertical slice."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock

from .command_receipt_bridge import CommandReceipt, CommandReceiptBridge
from .session_action_router import SessionActionRequest, SessionActionRouter
from .session_authority import SessionAuthority


class CommandConflict(ValueError):
    """Raised when a command id is reused with different canonical content."""


@dataclass(frozen=True, slots=True)
class RemoteControlResult:
    command_id: str
    action: str
    state: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _CommandRecord:
    digest: str
    result: RemoteControlResult


class RemoteControlFlowCoordinator:
    """Resolve a session, execute one allowed action, and publish one receipt."""

    def __init__(
        self,
        session_authority: SessionAuthority,
        action_router: SessionActionRouter,
        receipt_bridge: CommandReceiptBridge | None = None,
    ) -> None:
        self._session_authority = session_authority
        self._action_router = action_router
        self._receipt_bridge = receipt_bridge
        self._records: dict[str, _CommandRecord] = {}
        self._lock = RLock()

    def execute(
        self,
        *,
        command_id: str,
        runtime_generation: str,
        session_id: str,
        action: str,
        payload: Mapping[str, object] | None = None,
    ) -> RemoteControlResult:
        canonical_payload = dict(payload or {})
        digest = self._digest(
            command_id=command_id,
            runtime_generation=runtime_generation,
            session_id=session_id,
            action=action,
            payload=canonical_payload,
        )

        with self._lock:
            existing = self._records.get(command_id)
            if existing is not None:
                if existing.digest != digest:
                    raise CommandConflict("command id was reused with different content")
                return existing.result

            try:
                binding = self._session_authority.resolve(
                    session_id,
                    runtime_generation,
                )
            except KeyError:
                result = RemoteControlResult(
                    command_id=command_id,
                    action=action,
                    state="rejected",
                    detail="session_unavailable",
                )
            except ValueError:
                result = RemoteControlResult(
                    command_id=command_id,
                    action=action,
                    state="stale",
                    detail="runtime_generation_mismatch",
                )
            else:
                request = SessionActionRequest(
                    command_id=command_id,
                    action=action,
                    runtime_generation=runtime_generation,
                    session_id=session_id,
                    payload=canonical_payload,
                )
                try:
                    action_result = self._action_router.dispatch(
                        request,
                        binding.controller,
                    )
                except Exception:  # noqa: BLE001 - safe remote boundary
                    result = RemoteControlResult(
                        command_id=command_id,
                        action=action,
                        state="failed",
                        detail="action_failed",
                    )
                else:
                    result = RemoteControlResult(
                        command_id=command_id,
                        action=action,
                        state=action_result.state,
                        detail=action_result.detail,
                    )

            self._records[command_id] = _CommandRecord(digest=digest, result=result)
            self._publish_receipt(
                result=result,
                runtime_generation=runtime_generation,
                session_id=session_id,
            )
            return result

    def _publish_receipt(
        self,
        *,
        result: RemoteControlResult,
        runtime_generation: str,
        session_id: str,
    ) -> None:
        if self._receipt_bridge is None:
            return
        self._receipt_bridge.publish(
            CommandReceipt(
                command_id=result.command_id,
                runtime_generation=runtime_generation,
                session_id=session_id,
                state=result.state,
                detail=result.detail,
            )
        )

    @staticmethod
    def _digest(
        *,
        command_id: str,
        runtime_generation: str,
        session_id: str,
        action: str,
        payload: Mapping[str, object],
    ) -> str:
        try:
            encoded = json.dumps(
                {
                    "action": action,
                    "command_id": command_id,
                    "payload": payload,
                    "runtime_generation": runtime_generation,
                    "session_id": session_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("command payload must be canonical JSON") from error
        return hashlib.sha256(encoded).hexdigest()
