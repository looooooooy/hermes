"""Bounded one-shot process broker for direct Security.framework operations."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hermes_connector.adapters.platform.macos.keychain_errors import (
    KeychainBrokerEffectUnknown,
    KeychainSecretUnavailable,
)

_PROTOCOL_VERSION = 1
_REQUEST_HEADER = struct.Struct("!BB16sHHI")
_RESPONSE_HEADER = struct.Struct("!BB16sI")
_FRAME_HEADER = struct.Struct("!I")
_OPERATION_CODES = {
    "check": 1,
    "read": 2,
    "create": 3,
    "write": 4,
    "delete_if_digest": 5,
}
_CODE_OPERATIONS = {code: name for name, code in _OPERATION_CODES.items()}
_SUCCESS = 0
_FAILURE = 1
_MAX_REFERENCE_BYTES = 255
_MAX_PAYLOAD_BYTES = 16_413
_MAX_REQUEST_BYTES = _REQUEST_HEADER.size + 510 + _MAX_PAYLOAD_BYTES
_MAX_RESPONSE_BYTES = _RESPONSE_HEADER.size + 1 + _MAX_PAYLOAD_BYTES
_DEFAULT_OPERATION_TIMEOUT_SECONDS = 5.0
_JOIN_TIMEOUT_SECONDS = 0.05
_TRUSTED_HELPER_CWD = str(Path(sys.prefix).resolve())
_ENVELOPE_MAGIC = b"HERMESKC\x01"
_ENVELOPE_HEADER = struct.Struct("!9s16sI")
_MAX_LOGICAL_SECRET_BYTES = 16_384
_MUTATING_OPERATIONS = frozenset({"create", "write", "delete_if_digest"})

Operation = Literal["check", "read", "create", "write", "delete_if_digest"]
HelperCommand = tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class BrokerRequest:
    operation: Operation
    request_id: bytes
    service: bytes
    account: bytes
    payload: bytes

    def __repr__(self) -> str:
        return f"BrokerRequest(operation={self.operation!r}, payload=<redacted>)"


@dataclass(frozen=True, slots=True)
class _StoredRecord:
    raw: bytes
    payload: bytes
    revision: bytes | None


@dataclass(frozen=True, slots=True)
class _HelperInterrupted(Exception):
    cancelled: bool


def encode_broker_request(request: BrokerRequest) -> bytes:
    _validate_request(request)
    return (
        _REQUEST_HEADER.pack(
            _PROTOCOL_VERSION,
            _OPERATION_CODES[request.operation],
            request.request_id,
            len(request.service),
            len(request.account),
            len(request.payload),
        )
        + request.service
        + request.account
        + request.payload
    )


def decode_broker_request(value: bytes) -> BrokerRequest:
    if not isinstance(value, bytes) or len(value) < _REQUEST_HEADER.size:
        raise ValueError("Keychain broker request is invalid")
    (
        version,
        operation_code,
        request_id,
        service_length,
        account_length,
        payload_length,
    ) = _REQUEST_HEADER.unpack_from(value)
    expected_length = (
        _REQUEST_HEADER.size + service_length + account_length + payload_length
    )
    if version != _PROTOCOL_VERSION or expected_length != len(value):
        raise ValueError("Keychain broker request is invalid")
    operation = _CODE_OPERATIONS.get(operation_code)
    if operation is None:
        raise ValueError("Keychain broker request is invalid")
    service_start = _REQUEST_HEADER.size
    account_start = service_start + service_length
    payload_start = account_start + account_length
    request = BrokerRequest(
        operation=operation,
        request_id=request_id,
        service=value[service_start:account_start],
        account=value[account_start:payload_start],
        payload=value[payload_start:],
    )
    _validate_request(request)
    return request


def encode_broker_response(
    request_id: bytes,
    payload: bytes,
    *,
    success: bool = True,
) -> bytes:
    if (
        not isinstance(request_id, bytes)
        or len(request_id) != 16
        or not isinstance(payload, bytes)
        or len(payload) > 1 + _MAX_PAYLOAD_BYTES
    ):
        raise ValueError("Keychain broker response is invalid")
    return (
        _RESPONSE_HEADER.pack(
            _PROTOCOL_VERSION,
            _SUCCESS if success else _FAILURE,
            request_id,
            len(payload),
        )
        + payload
    )


def decode_broker_response(value: bytes, *, request_id: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < _RESPONSE_HEADER.size:
        raise ValueError("Keychain broker response is invalid")
    version, status, response_id, payload_length = _RESPONSE_HEADER.unpack_from(value)
    if (
        version != _PROTOCOL_VERSION
        or status not in {_SUCCESS, _FAILURE}
        or response_id != request_id
        or len(value) != _RESPONSE_HEADER.size + payload_length
        or payload_length > 1 + _MAX_PAYLOAD_BYTES
    ):
        raise ValueError("Keychain broker response is invalid")
    if status == _FAILURE:
        raise KeychainSecretUnavailable("macOS Keychain helper operation failed")
    return value[_RESPONSE_HEADER.size :]


def keychain_helper_stdio_main() -> None:
    from hermes_connector.adapters.platform.macos.keychain_direct import (
        MacOSDirectKeychainSecretStore,
    )

    request_id = b"\x00" * 16
    try:
        request = decode_broker_request(
            _read_frame_blocking(
                0,
                maximum_bytes=_MAX_REQUEST_BYTES,
            )
        )
        request_id = request.request_id
        direct = MacOSDirectKeychainSecretStore(
            service=request.service,
            account=request.account,
        )
        if request.operation == "check":
            direct.check_available()
            result = b""
        elif request.operation == "read":
            value = direct.read_raw()
            result = b"\x00" if value is None else b"\x01" + value
        elif request.operation == "create":
            result = b"\x01" if direct.create_raw(request.payload) else b"\x00"
        elif request.operation == "write":
            direct.write_raw(request.payload)
            result = b""
        elif request.operation == "delete_if_digest":
            deleted = direct.delete_raw_if_digest(request.payload)
            result = b"\x01" if deleted else b"\x00"
        else:
            raise ValueError("Keychain broker operation is invalid")
        response = encode_broker_response(request_id, result)
    except Exception:  # noqa: BLE001 - child emits only a redacted failure bit
        response = encode_broker_response(request_id, b"", success=False)
    try:
        _write_frame_blocking(1, response)
    except (BrokenPipeError, EOFError, OSError):
        pass


class MacOSKeychainBroker:
    """Serialize bounded helper processes and reconcile uncertain mutations."""

    __slots__ = (
        "_active_process",
        "_closed",
        "_helper_command",
        "_lock",
        "_operation_timeout_seconds",
        "_started",
        "_stop_event",
    )

    def __init__(
        self,
        *,
        operation_timeout_seconds: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
        helper_command: HelperCommand | None = None,
    ) -> None:
        if (
            not isinstance(operation_timeout_seconds, int | float)
            or isinstance(operation_timeout_seconds, bool)
            or not 0 < operation_timeout_seconds <= 30
            or helper_command is not None
            and (
                not isinstance(helper_command, tuple)
                or not helper_command
                or any(
                    not isinstance(value, str) or not value for value in helper_command
                )
            )
        ):
            raise ValueError("Keychain broker configuration is invalid")
        self._operation_timeout_seconds = float(operation_timeout_seconds)
        self._helper_command = helper_command or (
            sys.executable,
            "-I",
            "-m",
            "hermes_connector.adapters.platform.macos.keychain_helper",
        )
        self._lock = asyncio.Lock()
        self._active_process = None
        self._closed = False
        self._started = False
        self._stop_event = asyncio.Event()

    @property
    def name(self) -> str:
        return "keychain_broker"

    async def start(self) -> None:
        if self._closed:
            raise KeychainSecretUnavailable("macOS Keychain broker is closed")
        self._started = True

    async def ready(self) -> bool:
        return self._started and not self._closed

    async def run(self) -> None:
        await self._stop_event.wait()

    async def drain(self) -> None:
        return None

    async def stop(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        self._stop_event.set()
        process = self._active_process
        if process is not None:
            cancelled = await _terminate_process_resisting_cancellation(process)
            if cancelled:
                raise asyncio.CancelledError

    def check_available(self) -> None:
        if self._closed:
            raise KeychainSecretUnavailable("macOS Keychain broker is closed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise KeychainSecretUnavailable(
                "macOS Keychain availability check requires sync startup"
            )
        asyncio.run(self._check_available())

    async def _check_available(self) -> None:
        request = BrokerRequest(
            operation="check",
            request_id=secrets.token_bytes(16),
            service=b"wiki.seaotter.hermes.connector.availability",
            account=b"availability-check",
            payload=b"",
        )
        deadline = asyncio.get_running_loop().time() + self._operation_timeout_seconds
        try:
            await self._execute(request, deadline=deadline)
        except _HelperInterrupted:
            raise KeychainSecretUnavailable(
                "macOS Keychain availability check failed"
            ) from None

    async def read_secret(self, service: bytes, account: bytes) -> bytes | None:
        async with self._lock:
            deadline = (
                asyncio.get_running_loop().time() + self._operation_timeout_seconds
            )
            raw = await self._read_raw(service, account, deadline=deadline)
            if raw is None:
                return None
            return _decode_stored_record(raw).payload

    async def create_secret(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> bool:
        async with self._lock:
            loop = asyncio.get_running_loop()
            primary_deadline = loop.time() + self._operation_timeout_seconds
            target = _encode_envelope(secret)
            request = self._request("create", service, account, target)
            try:
                response = await self._execute(request, deadline=primary_deadline)
            except _HelperInterrupted as interruption:
                recovery_deadline = loop.time() + self._operation_timeout_seconds
                confirmed = await self._confirm_target(
                    service,
                    account,
                    target,
                    deadline=recovery_deadline,
                )
                if interruption.cancelled:
                    raise asyncio.CancelledError
                if confirmed:
                    return True
                raise KeychainSecretUnavailable(
                    "macOS Keychain create did not converge"
                ) from None
            return _decode_boolean(response)

    async def write_secret(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            primary_deadline = loop.time() + self._operation_timeout_seconds
            target = _encode_envelope(secret)
            request = self._request("write", service, account, target)
            try:
                await self._execute(request, deadline=primary_deadline)
            except _HelperInterrupted as interruption:
                recovery_deadline = loop.time() + self._operation_timeout_seconds
                confirmed = await self._confirm_target(
                    service,
                    account,
                    target,
                    deadline=recovery_deadline,
                )
                if interruption.cancelled:
                    raise asyncio.CancelledError
                if confirmed:
                    return
                raise KeychainSecretUnavailable(
                    "macOS Keychain write did not converge"
                ) from None

    async def delete_secret(self, service: bytes, account: bytes) -> bool:
        async with self._lock:
            deadline = (
                asyncio.get_running_loop().time() + self._operation_timeout_seconds
            )
            raw = await self._read_raw(service, account, deadline=deadline)
            if raw is None:
                return False
            return await self._delete_expected(
                service,
                account,
                raw,
                primary_deadline=deadline,
            )

    async def delete_secret_if_matches(
        self,
        service: bytes,
        account: bytes,
        *,
        expected_sha256: bytes,
    ) -> bool:
        async with self._lock:
            deadline = (
                asyncio.get_running_loop().time() + self._operation_timeout_seconds
            )
            raw = await self._read_raw(service, account, deadline=deadline)
            if raw is None:
                return False
            record = _decode_stored_record(raw)
            if not secrets.compare_digest(
                hashlib.sha256(record.payload).digest(),
                expected_sha256,
            ):
                return False
            return await self._delete_expected(
                service,
                account,
                raw,
                primary_deadline=deadline,
            )

    async def _delete_expected(
        self,
        service: bytes,
        account: bytes,
        raw: bytes,
        *,
        primary_deadline: float,
    ) -> bool:
        expected_raw_digest = hashlib.sha256(raw).digest()
        request = self._request(
            "delete_if_digest",
            service,
            account,
            expected_raw_digest,
        )
        try:
            response = await self._execute(request, deadline=primary_deadline)
        except _HelperInterrupted as interruption:
            recovery_deadline = (
                asyncio.get_running_loop().time() + self._operation_timeout_seconds
            )
            current = await self._recover_read_raw(
                service,
                account,
                deadline=recovery_deadline,
            )
            if current is None:
                deleted = True
            elif not secrets.compare_digest(
                hashlib.sha256(current).digest(),
                expected_raw_digest,
            ):
                deleted = False
            else:
                retry = self._request(
                    "delete_if_digest",
                    service,
                    account,
                    expected_raw_digest,
                )
                try:
                    deleted = _decode_boolean(
                        await self._execute(retry, deadline=recovery_deadline)
                    )
                except _HelperInterrupted:
                    raise KeychainBrokerEffectUnknown(
                        "macOS Keychain delete effect is unknown"
                    ) from None
            if interruption.cancelled:
                raise asyncio.CancelledError
            return deleted
        return _decode_boolean(response)

    async def _confirm_target(
        self,
        service: bytes,
        account: bytes,
        target: bytes,
        *,
        deadline: float,
    ) -> bool:
        current = await self._recover_read_raw(
            service,
            account,
            deadline=deadline,
        )
        return current is not None and secrets.compare_digest(current, target)

    async def _read_raw(
        self,
        service: bytes,
        account: bytes,
        *,
        deadline: float,
    ) -> bytes | None:
        request = self._request("read", service, account, b"")
        try:
            return _decode_optional(await self._execute(request, deadline=deadline))
        except _HelperInterrupted as interruption:
            if interruption.cancelled:
                raise asyncio.CancelledError
            raise KeychainSecretUnavailable("macOS Keychain read timed out") from None

    async def _recover_read_raw(
        self,
        service: bytes,
        account: bytes,
        *,
        deadline: float,
    ) -> bytes | None:
        request = self._request("read", service, account, b"")
        try:
            return _decode_optional(await self._execute(request, deadline=deadline))
        except (KeychainSecretUnavailable, _HelperInterrupted):
            raise KeychainBrokerEffectUnknown(
                "macOS Keychain mutation effect is unknown"
            ) from None

    def _request(
        self,
        operation: Operation,
        service: bytes,
        account: bytes,
        payload: bytes,
    ) -> BrokerRequest:
        return BrokerRequest(
            operation=operation,
            request_id=secrets.token_bytes(16),
            service=service,
            account=account,
            payload=payload,
        )

    async def _execute(
        self,
        request: BrokerRequest,
        *,
        deadline: float,
    ) -> bytes:
        if self._closed:
            raise KeychainSecretUnavailable("macOS Keychain broker is closed")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _HelperInterrupted(cancelled=False)
        process = None
        try:
            process = await self._start_process(deadline=deadline)
            self._active_process = process
            if process.stdin is None or process.stdout is None:
                raise OSError("Keychain helper pipes are unavailable")
            async with asyncio.timeout_at(deadline):
                process.stdin.write(_frame(encode_broker_request(request)))
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
                response = await _read_frame_stream(
                    process.stdout,
                    maximum_bytes=_MAX_RESPONSE_BYTES,
                )
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process_resisting_cancellation(process)
            raise _HelperInterrupted(cancelled=True) from None
        except (TimeoutError, OSError, EOFError, ValueError):
            cancelled = False
            if process is not None:
                cancelled = await _terminate_process_resisting_cancellation(process)
            raise _HelperInterrupted(cancelled=cancelled) from None
        except Exception:  # noqa: BLE001 - process boundary is redacted
            cancelled = False
            if process is not None:
                cancelled = await _terminate_process_resisting_cancellation(process)
            raise _HelperInterrupted(cancelled=cancelled) from None
        finally:
            if process is not None and process.stdin is not None:
                process.stdin.close()
            self._active_process = None

        cancelled = await _terminate_process_resisting_cancellation(
            process,
            terminate=False,
        )
        if cancelled:
            raise _HelperInterrupted(cancelled=True)
        try:
            return decode_broker_response(response, request_id=request.request_id)
        except (KeychainSecretUnavailable, ValueError):
            if request.operation in _MUTATING_OPERATIONS:
                raise _HelperInterrupted(cancelled=False) from None
            raise KeychainSecretUnavailable(
                "macOS Keychain helper response is invalid"
            ) from None

    async def _start_process(self, *, deadline: float):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self._helper_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PYTHONNOUSERSITE": "1"},
                cwd=_TRUSTED_HELPER_CWD,
                start_new_session=True,
            )
        )
        try:
            return await asyncio.wait_for(launch, timeout=remaining)
        except asyncio.CancelledError:
            await _cancel_launch_resisting_cancellation(launch)
            raise
        except Exception:
            cancelled = await _cancel_launch_resisting_cancellation(launch)
            if cancelled:
                raise asyncio.CancelledError from None
            raise

    def __repr__(self) -> str:
        return "MacOSKeychainBroker(<redacted>)"


async def _read_frame_stream(
    stream: asyncio.StreamReader,
    *,
    maximum_bytes: int,
) -> bytes:
    header = await stream.readexactly(_FRAME_HEADER.size)
    (payload_length,) = _FRAME_HEADER.unpack(header)
    if payload_length > maximum_bytes:
        raise ValueError("Keychain helper frame is oversized")
    return await stream.readexactly(payload_length)


async def _terminate_process(process, *, terminate: bool = True) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    if terminate:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_JOIN_TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_JOIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise KeychainBrokerEffectUnknown(
                "macOS Keychain helper could not be reaped"
            ) from None


async def _cancel_launch_and_reap(launch) -> None:
    launch.cancel()
    try:
        process = await launch
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001 - launch failure is handled by caller
        return
    await _terminate_process(process)


async def _cancel_launch_resisting_cancellation(launch) -> bool:
    cleanup = asyncio.create_task(_cancel_launch_and_reap(launch))
    return await _wait_for_cleanup(cleanup)


async def _terminate_process_resisting_cancellation(
    process,
    *,
    terminate: bool = True,
) -> bool:
    cleanup = asyncio.create_task(
        _terminate_process(
            process,
            terminate=terminate,
        )
    )
    return await _wait_for_cleanup(cleanup)


async def _wait_for_cleanup(cleanup: asyncio.Task[None]) -> bool:
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            return cancelled
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            cancelled = True
            if cleanup.done():
                cleanup.result()
                return cancelled


def _frame(payload: bytes) -> bytes:
    return _FRAME_HEADER.pack(len(payload)) + payload


def _read_frame_blocking(
    descriptor: int,
    *,
    maximum_bytes: int,
) -> bytes:
    header = _read_exact_blocking(descriptor, _FRAME_HEADER.size)
    (payload_length,) = _FRAME_HEADER.unpack(header)
    if payload_length > maximum_bytes:
        raise ValueError("Keychain helper frame is oversized")
    return _read_exact_blocking(descriptor, payload_length)


def _read_exact_blocking(descriptor: int, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_frame_blocking(descriptor: int, payload: bytes) -> None:
    framed = memoryview(_frame(payload))
    while framed:
        written = os.write(descriptor, framed)
        framed = framed[written:]


def _validate_request(request: BrokerRequest) -> None:
    if (
        not isinstance(request, BrokerRequest)
        or request.operation not in _OPERATION_CODES
        or not isinstance(request.request_id, bytes)
        or len(request.request_id) != 16
        or not isinstance(request.service, bytes)
        or not 1 <= len(request.service) <= _MAX_REFERENCE_BYTES
        or not isinstance(request.account, bytes)
        or not 1 <= len(request.account) <= _MAX_REFERENCE_BYTES
        or any(
            byte < 0x20
            for reference in (request.service, request.account)
            for byte in reference
        )
        or not isinstance(request.payload, bytes)
        or len(request.payload) > _MAX_PAYLOAD_BYTES
    ):
        raise ValueError("Keychain broker request is invalid")
    if request.operation in {"check", "read"} and request.payload:
        raise ValueError("Keychain broker request is invalid")
    if request.operation in {"create", "write"} and not request.payload:
        raise ValueError("Keychain broker request is invalid")
    if request.operation == "delete_if_digest" and len(request.payload) != 32:
        raise ValueError("Keychain broker request is invalid")


def _encode_envelope(secret: bytes) -> bytes:
    if not 1 <= len(secret) <= _MAX_LOGICAL_SECRET_BYTES:
        raise KeychainSecretUnavailable("macOS Keychain secret is invalid")
    revision = secrets.token_bytes(16)
    return (
        _ENVELOPE_HEADER.pack(
            _ENVELOPE_MAGIC,
            revision,
            len(secret),
        )
        + secret
    )


def _decode_stored_record(raw: bytes) -> _StoredRecord:
    if not raw.startswith(_ENVELOPE_MAGIC):
        return _StoredRecord(raw=raw, payload=raw, revision=None)
    if len(raw) < _ENVELOPE_HEADER.size:
        raise KeychainSecretUnavailable("macOS Keychain content is invalid")
    magic, revision, payload_length = _ENVELOPE_HEADER.unpack_from(raw)
    if (
        magic != _ENVELOPE_MAGIC
        or payload_length < 1
        or payload_length > _MAX_LOGICAL_SECRET_BYTES
        or len(raw) != _ENVELOPE_HEADER.size + payload_length
    ):
        raise KeychainSecretUnavailable("macOS Keychain content is invalid")
    return _StoredRecord(
        raw=raw,
        payload=raw[_ENVELOPE_HEADER.size :],
        revision=revision,
    )


def _decode_optional(value: bytes) -> bytes | None:
    if value == b"\x00":
        return None
    if len(value) >= 2 and value.startswith(b"\x01"):
        return value[1:]
    raise KeychainSecretUnavailable("macOS Keychain helper response is invalid")


def _decode_boolean(value: bytes) -> bool:
    if value == b"\x00":
        return False
    if value == b"\x01":
        return True
    raise KeychainSecretUnavailable("macOS Keychain helper response is invalid")
