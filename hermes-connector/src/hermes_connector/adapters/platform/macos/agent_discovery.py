from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import stat
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

from hermes_connector.adapters.contract_codec import InvalidEnvelope
from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    current_process_identity,
    normalize_process_identity,
)
from hermes_connector.domain.local_gateway import (
    DISCOVERY_DESCRIPTOR_FIELDS,
    DISCOVERY_DESCRIPTOR_VERSION,
    AgentEndpoint,
    ProcessIdentityEvidence,
)

MAX_DESCRIPTOR_BYTES = 16_384
DEFAULT_MAX_CANDIDATES = 32
_MAX_DIRECTORY_ENTRIES = 64

_DESCRIPTOR_VERSION = DISCOVERY_DESCRIPTOR_VERSION
_DESCRIPTOR_FIELDS = DISCOVERY_DESCRIPTOR_FIELDS
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_CANONICAL_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_MAX_NATIVE_UDS_PATH_BYTES = 103
_DISCOVERY_WORKERS = 1
_BUNDLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")


class MacOSAgentDiscovery:
    """Read the Plugin's bounded, atomically published macOS UDS descriptors.

    Security decision flow:

        trusted dirs -> bounded snapshot -> no-follow descriptor read
             -> exact v2 schema -> exact process -> trusted socket -> endpoint

    Every invalid candidate is ignored independently. Discovery never repairs
    or deletes a descriptor because deletion needs a second identity check that
    belongs to the Plugin lifecycle owner.
    """

    def __init__(
        self,
        registry_directory: Path,
        socket_directory: Path,
        *,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        max_descriptor_bytes: int = MAX_DESCRIPTOR_BYTES,
        effective_uid: int | None = None,
        pid_is_alive: Callable[[int], bool] | None = None,
        process_identity_provider: ProcessIdentityProvider | None = None,
    ) -> None:
        if (
            type(max_candidates) is not int
            or max_candidates <= 0
            or type(max_descriptor_bytes) is not int
            or max_descriptor_bytes <= 0
        ):
            raise ValueError("discovery bounds must be positive integers")
        self._registry_directory = _absolute_lexical(registry_directory)
        self._socket_directory = _absolute_lexical(socket_directory)
        self._max_candidates = max_candidates
        self._max_descriptor_bytes = max_descriptor_bytes
        self._effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self._pid_is_alive = pid_is_alive or _pid_is_alive
        self._process_identity_provider = (
            process_identity_provider or current_process_identity
        )
        self._executor: ThreadPoolExecutor | None = None
        self._executor_guard = threading.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def discover(self, profile: str) -> tuple[AgentEndpoint, ...]:
        if not isinstance(profile, str) or _PROFILE_PATTERN.fullmatch(profile) is None:
            return ()
        loop = asyncio.get_running_loop()
        with self._executor_guard:
            if self._closed:
                return ()
            executor = self._executor
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=_DISCOVERY_WORKERS,
                    thread_name_prefix="hermes-agent-discovery",
                )
                self._executor = executor
            operation = loop.run_in_executor(executor, self._discover_sync, profile)
        return await operation

    def discover_now(self, profile: str) -> tuple[AgentEndpoint, ...]:
        """Perform the same bounded read-only proof without creating a worker."""

        if not isinstance(profile, str) or _PROFILE_PATTERN.fullmatch(profile) is None:
            return ()
        with self._executor_guard:
            if self._closed:
                return ()
        return self._discover_sync(profile)

    async def aclose(self) -> None:
        close_task = self._close_task
        if close_task is None:
            with self._executor_guard:
                self._closed = True
                executor = self._executor
                self._executor = None
                barrier: Future[None] | None = None
                if executor is not None:
                    barrier = executor.submit(_worker_barrier)
                    executor.shutdown(wait=False, cancel_futures=False)
            close_task = asyncio.create_task(
                _finish_executor_shutdown(executor, barrier),
                name="hermes-connector:agent-discovery-close",
            )
            self._close_task = close_task
        await _wait_for_cleanup(close_task)

    def _discover_sync(self, profile: str) -> tuple[AgentEndpoint, ...]:
        registry_fd = self._open_trusted_directory(self._registry_directory)
        if registry_fd is None:
            return ()
        socket_fd = self._open_trusted_directory(self._socket_directory)
        if socket_fd is None:
            os.close(registry_fd)
            return ()

        try:
            names = _bounded_descriptor_names(
                registry_fd,
                max_candidates=self._max_candidates,
            )
            if names is None:
                return ()
            endpoints: list[AgentEndpoint] = []
            for name in names:
                endpoint = self._read_candidate(
                    registry_fd,
                    socket_fd,
                    name,
                    profile,
                )
                if endpoint is not None:
                    endpoints.append(endpoint)
            return tuple(endpoints)
        finally:
            os.close(socket_fd)
            os.close(registry_fd)

    def _open_trusted_directory(self, path: Path) -> int | None:
        try:
            descriptor = os.open(path, _DIRECTORY_FLAGS)
        except OSError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not self._has_trusted_metadata(
                metadata,
                expected_mode=0o700,
                type_check=stat.S_ISDIR,
            ):
                os.close(descriptor)
                return None
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _read_candidate(
        self,
        registry_fd: int,
        socket_fd: int,
        name: str,
        profile: str,
    ) -> AgentEndpoint | None:
        try:
            before = os.stat(name, dir_fd=registry_fd, follow_symlinks=False)
            if not self._has_trusted_metadata(
                before,
                expected_mode=0o600,
                type_check=stat.S_ISREG,
            ):
                return None
            descriptor_fd = os.open(name, _FILE_FLAGS, dir_fd=registry_fd)
        except OSError:
            return None

        try:
            opened = os.fstat(descriptor_fd)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or not self._has_trusted_metadata(
                    opened,
                    expected_mode=0o600,
                    type_check=stat.S_ISREG,
                )
                or not 1 <= opened.st_size <= self._max_descriptor_bytes
            ):
                return None
            raw = _read_bounded(
                descriptor_fd,
                maximum=self._max_descriptor_bytes,
            )
            after = os.fstat(descriptor_fd)
            if (
                not _stable_file(opened, after)
                or not self._has_trusted_metadata(
                    after,
                    expected_mode=0o600,
                    type_check=stat.S_ISREG,
                )
                or len(raw) != opened.st_size
            ):
                return None
            value = _decode_descriptor(raw)
            parsed = _parse_descriptor(value, expected_profile=profile)
            if parsed is None:
                return None
            (
                pid,
                runtime_generation,
                socket_path,
                instance_id,
                host_bundle_id,
                expected_process,
            ) = parsed
            if not self._pid_is_alive(pid):
                return None
            if not self._process_matches(pid, expected_process):
                return None
            if socket_path.parent != self._socket_directory:
                return None
            if len(os.fsencode(socket_path)) > _MAX_NATIVE_UDS_PATH_BYTES:
                return None
            socket_metadata = self._trusted_socket(socket_fd, socket_path.name)
            if socket_metadata is None:
                return None
            if not self._process_matches(pid, expected_process):
                return None
            return AgentEndpoint(
                pid=pid,
                profile=profile,
                socket_path=socket_path,
                instance_id=instance_id,
                runtime_generation=runtime_generation,
                host_bundle_id=host_bundle_id,
                process_identity=expected_process,
                socket_device=socket_metadata.st_dev,
                socket_inode=socket_metadata.st_ino,
                registry_path=self._registry_directory / name,
            )
        except (InvalidEnvelope, OSError, UnicodeError, ValueError):
            return None
        finally:
            os.close(descriptor_fd)

    def _trusted_socket(self, socket_fd: int, name: str) -> os.stat_result | None:
        if not name or "/" in name or name in {".", ".."}:
            return None
        try:
            metadata = os.stat(name, dir_fd=socket_fd, follow_symlinks=False)
        except OSError:
            return None
        if not self._has_trusted_metadata(
            metadata,
            expected_mode=0o600,
            type_check=stat.S_ISSOCK,
        ):
            return None
        return metadata

    def _process_matches(
        self,
        pid: int,
        expected: ProcessIdentityEvidence,
    ) -> bool:
        try:
            observed = self._process_identity_provider(pid)
        except BaseException:  # noqa: BLE001 - process evidence boundary
            return False
        return normalize_process_identity(observed) == expected

    def _has_trusted_metadata(
        self,
        metadata: os.stat_result,
        *,
        expected_mode: int,
        type_check: Callable[[int], bool],
    ) -> bool:
        return (
            type_check(metadata.st_mode)
            and metadata.st_uid == self._effective_uid
            and stat.S_IMODE(metadata.st_mode) == expected_mode
        )


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _worker_barrier() -> None:
    return None


async def _finish_executor_shutdown(
    executor: ThreadPoolExecutor | None,
    barrier: Future[None] | None,
) -> None:
    if executor is None or barrier is None:
        return
    try:
        await asyncio.wrap_future(barrier)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


async def _wait_for_cleanup(cleanup: asyncio.Task[None]) -> None:
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            if cancelled:
                raise asyncio.CancelledError
            return
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            cancelled = True
            if cleanup.done():
                cleanup.result()
                raise


def _read_bounded(descriptor: int, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if not 1 <= len(value) <= maximum:
        raise InvalidEnvelope("discovery descriptor size is outside limits")
    return value


def _bounded_descriptor_names(
    registry_fd: int,
    *,
    max_candidates: int,
) -> tuple[str, ...] | None:
    names: list[str] = []
    scanned = 0
    try:
        with os.scandir(registry_fd) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_DIRECTORY_ENTRIES:
                    return None
                if entry.name.startswith(".") or not entry.name.endswith(".json"):
                    continue
                names.append(entry.name)
                if len(names) > max_candidates:
                    return None
    except OSError:
        return None
    names.sort()
    return tuple(names)


def _stable_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_size == after.st_size
    )


def _decode_descriptor(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_non_json_number,
        )
    except InvalidEnvelope:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise InvalidEnvelope("discovery descriptor must be strict JSON") from None
    if not isinstance(value, dict):
        raise InvalidEnvelope("discovery descriptor must be an object")
    return value


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InvalidEnvelope("duplicate discovery field is not allowed")
        value[key] = item
    return value


def _reject_non_json_number(value: str) -> None:
    raise InvalidEnvelope(f"non-JSON number is not allowed: {value}")


def _parse_descriptor(
    value: dict[str, Any],
    *,
    expected_profile: str,
) -> tuple[int, str, Path, str, str, ProcessIdentityEvidence] | None:
    if frozenset(value) != _DESCRIPTOR_FIELDS:
        return None
    if type(value["version"]) is not int or value["version"] != _DESCRIPTOR_VERSION:
        return None
    pid = value["pid"]
    if type(pid) is not int or not 1 <= pid <= 2_147_483_647:
        return None
    profile = value["profile"]
    if (
        not isinstance(profile, str)
        or _PROFILE_PATTERN.fullmatch(profile) is None
        or profile != expected_profile
    ):
        return None
    runtime_generation = value["runtime_generation"]
    if (
        not isinstance(runtime_generation, str)
        or not 1 <= len(runtime_generation) <= 128
        or runtime_generation != runtime_generation.strip()
        or "\x00" in runtime_generation
    ):
        return None
    socket_value = value["socket_path"]
    if (
        not isinstance(socket_value, str)
        or not 2 <= len(socket_value) <= 4096
        or "\x00" in socket_value
    ):
        return None
    socket_path = Path(socket_value)
    if not socket_path.is_absolute() or ".." in socket_path.parts:
        return None
    instance_id = value["instance_id"]
    if (
        not isinstance(instance_id, str)
        or _CANONICAL_UUID_PATTERN.fullmatch(instance_id) is None
    ):
        return None
    try:
        if str(UUID(instance_id)) != instance_id:
            return None
    except ValueError:
        return None
    executable_value = value["process_executable"]
    if (
        not isinstance(executable_value, str)
        or not 2 <= len(executable_value) <= 4096
        or "\x00" in executable_value
    ):
        return None
    executable_path = Path(executable_value)
    if not executable_path.is_absolute() or ".." in executable_path.parts:
        return None
    expected_process = normalize_process_identity(
        ProcessIdentityEvidence(
            start_time_ns=value["process_start_time_ns"],
            executable_path=executable_path,
            executable_device=value["process_executable_device"],
            executable_inode=value["process_executable_inode"],
        )
    )
    host_bundle_id = value["host_bundle_id"]
    if (
        expected_process is None
        or not isinstance(host_bundle_id, str)
        or _BUNDLE_ID_PATTERN.fullmatch(host_bundle_id) is None
    ):
        return None
    return (
        pid,
        runtime_generation,
        socket_path,
        instance_id,
        host_bundle_id,
        expected_process,
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as error:
        if error.errno == errno.EPERM:
            return True
        if error.errno == errno.ESRCH:
            return False
        return False
    return True
