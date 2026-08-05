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
from dataclasses import dataclass
from pathlib import Path

from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    current_process_identity,
    normalize_process_identity,
)
from hermes_connector.domain.local_gateway import (
    DISCOVERY_DESCRIPTOR_FIELDS,
    DISCOVERY_DESCRIPTOR_VERSION,
    ProcessIdentityEvidence,
)

MAX_OBSERVER_DESCRIPTOR_BYTES = 16_384
DEFAULT_MAX_OBSERVER_CANDIDATES = 32
_MAX_DIRECTORY_ENTRIES = 64
_MAX_NATIVE_UDS_PATH_BYTES = 103
_DISCOVERY_WORKERS = 1
_DESCRIPTOR_VERSION = DISCOVERY_DESCRIPTOR_VERSION
_FIELDS = DISCOVERY_DESCRIPTOR_FIELDS
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_UUID_CANONICAL = re.compile(
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
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ObserverEndpoint:
    pid: int
    profile: str
    runtime_generation: str
    socket_path: Path
    instance_id: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidence
    socket_device: int
    socket_inode: int
    registry_path: Path


class MacOSObserverEndpointDiscovery:
    """Read only the generation-bound Plugin Observer descriptor schema."""

    def __init__(
        self,
        registry_directory: Path,
        socket_directory: Path,
        *,
        max_candidates: int = DEFAULT_MAX_OBSERVER_CANDIDATES,
        max_descriptor_bytes: int = MAX_OBSERVER_DESCRIPTOR_BYTES,
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
            raise ValueError("Observer discovery bounds must be positive integers")
        self.registry_directory = Path(os.path.abspath(os.fspath(registry_directory)))
        self.socket_directory = Path(os.path.abspath(os.fspath(socket_directory)))
        self._max_candidates = max_candidates
        self._max_descriptor_bytes = max_descriptor_bytes
        self._effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self._pid_is_alive = pid_is_alive or _pid_is_alive
        self._process_identity_provider = (
            process_identity_provider or current_process_identity
        )
        self._executor: ThreadPoolExecutor | None = None
        self._guard = threading.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    async def discover(self, profile: str) -> tuple[ObserverEndpoint, ...]:
        if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
            return ()
        loop = asyncio.get_running_loop()
        with self._guard:
            if self._closed:
                return ()
            executor = self._executor
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=_DISCOVERY_WORKERS,
                    thread_name_prefix="hermes-observer-discovery",
                )
                self._executor = executor
            operation = loop.run_in_executor(executor, self._discover_sync, profile)
        return await operation

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            with self._guard:
                self._closed = True
                executor = self._executor
                self._executor = None
                barrier: Future[None] | None = None
                if executor is not None:
                    barrier = executor.submit(_barrier)
                    executor.shutdown(wait=False, cancel_futures=False)
            task = asyncio.create_task(
                _finish_shutdown(executor, barrier),
                name="hermes-connector:observer-discovery-close",
            )
            self._close_task = task
        await _wait_cleanup(task)

    def _discover_sync(self, profile: str) -> tuple[ObserverEndpoint, ...]:
        registry_fd = self._open_directory(self.registry_directory)
        if registry_fd is None:
            return ()
        socket_fd = self._open_directory(self.socket_directory)
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
            return tuple(
                endpoint
                for name in names
                if (
                    endpoint := self._read_candidate(
                        registry_fd,
                        socket_fd,
                        name,
                        profile,
                    )
                )
                is not None
            )
        finally:
            os.close(socket_fd)
            os.close(registry_fd)

    def _open_directory(self, path: Path) -> int | None:
        try:
            descriptor = os.open(path, _DIRECTORY_FLAGS)
        except OSError:
            return None
        metadata = os.fstat(descriptor)
        if not self._trusted(metadata, mode=0o700, kind=stat.S_ISDIR):
            os.close(descriptor)
            return None
        return descriptor

    def _read_candidate(
        self,
        registry_fd: int,
        socket_fd: int,
        name: str,
        expected_profile: str,
    ) -> ObserverEndpoint | None:
        try:
            before = os.stat(name, dir_fd=registry_fd, follow_symlinks=False)
            if not self._trusted(before, mode=0o600, kind=stat.S_ISREG):
                return None
            descriptor_fd = os.open(name, _FILE_FLAGS, dir_fd=registry_fd)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor_fd)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or not self._trusted(opened, mode=0o600, kind=stat.S_ISREG)
                or not 1 <= opened.st_size <= self._max_descriptor_bytes
            ):
                return None
            raw = _read(descriptor_fd, self._max_descriptor_bytes)
            after = os.fstat(descriptor_fd)
            if (
                not _stable_file(opened, after)
                or not self._trusted(after, mode=0o600, kind=stat.S_ISREG)
                or len(raw) != opened.st_size
            ):
                return None
            value = _decode(raw)
            parsed = _parse(value, expected_profile=expected_profile)
            if parsed is None:
                return None
            (
                pid,
                profile,
                generation,
                socket_path,
                instance_id,
                host_bundle_id,
                expected_process,
            ) = parsed
            if (
                not self._pid_is_alive(pid)
                or not self._process_matches(pid, expected_process)
                or socket_path.parent != self.socket_directory
                or len(os.fsencode(socket_path)) > _MAX_NATIVE_UDS_PATH_BYTES
            ):
                return None
            try:
                socket_metadata = os.stat(
                    socket_path.name,
                    dir_fd=socket_fd,
                    follow_symlinks=False,
                )
            except OSError:
                return None
            if not self._trusted(socket_metadata, mode=0o600, kind=stat.S_ISSOCK):
                return None
            if not self._process_matches(pid, expected_process):
                return None
            return ObserverEndpoint(
                pid=pid,
                profile=profile,
                runtime_generation=generation,
                socket_path=socket_path,
                instance_id=instance_id,
                host_bundle_id=host_bundle_id,
                process_identity=expected_process,
                socket_device=socket_metadata.st_dev,
                socket_inode=socket_metadata.st_ino,
                registry_path=self.registry_directory / name,
            )
        except (OSError, UnicodeError, ValueError):
            return None
        finally:
            os.close(descriptor_fd)

    def _trusted(
        self,
        metadata: os.stat_result,
        *,
        mode: int,
        kind: Callable[[int], bool],
    ) -> bool:
        return (
            kind(metadata.st_mode)
            and metadata.st_uid == self._effective_uid
            and stat.S_IMODE(metadata.st_mode) == mode
        )

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


def _read(descriptor: int, maximum: int) -> bytes:
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
        raise ValueError("Observer descriptor size is outside limits")
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


def _decode(raw: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate Observer descriptor field")
            value[key] = item
        return value

    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=unique,
        parse_constant=_reject_non_json_number,
    )
    if not isinstance(value, dict):
        raise TypeError("Observer descriptor must be an object")
    return value


def _reject_non_json_number(_value: str) -> None:
    raise ValueError("Observer descriptor contains a non-JSON number")


def _parse(
    value: dict[str, object],
    *,
    expected_profile: str,
) -> (
    tuple[
        int,
        str,
        str,
        Path,
        str,
        str,
        ProcessIdentityEvidence,
    ]
    | None
):
    version = value.get("version")
    if (
        frozenset(value) != _FIELDS
        or type(version) is not int
        or version != _DESCRIPTOR_VERSION
    ):
        return None
    pid = value["pid"]
    profile = value["profile"]
    generation = value["runtime_generation"]
    socket_value = value["socket_path"]
    instance_id = value["instance_id"]
    if type(pid) is not int or not 1 <= pid <= 2_147_483_647:
        return None
    if (
        not isinstance(profile, str)
        or _PROFILE.fullmatch(profile) is None
        or profile != expected_profile
    ):
        return None
    if (
        not isinstance(generation, str)
        or not 1 <= len(generation) <= 128
        or generation != generation.strip()
        or "\x00" in generation
    ):
        return None
    if (
        not isinstance(socket_value, str)
        or not 2 <= len(socket_value) <= 4096
        or "\x00" in socket_value
    ):
        return None
    socket_path = Path(socket_value)
    if not socket_path.is_absolute() or ".." in socket_path.parts:
        return None
    if (
        not isinstance(instance_id, str)
        or _UUID_CANONICAL.fullmatch(instance_id) is None
    ):
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
    process_identity = normalize_process_identity(
        ProcessIdentityEvidence(
            start_time_ns=value["process_start_time_ns"],
            executable_path=executable_path,
            executable_device=value["process_executable_device"],
            executable_inode=value["process_executable_inode"],
        )
    )
    host_bundle_id = value["host_bundle_id"]
    if (
        process_identity is None
        or not isinstance(host_bundle_id, str)
        or _BUNDLE_ID.fullmatch(host_bundle_id) is None
    ):
        return None
    return (
        pid,
        profile,
        generation,
        socket_path,
        instance_id,
        host_bundle_id,
        process_identity,
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _barrier() -> None:
    return None


async def _finish_shutdown(
    executor: ThreadPoolExecutor | None,
    barrier: Future[None] | None,
) -> None:
    if executor is None or barrier is None:
        return
    try:
        await asyncio.wrap_future(barrier)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


async def _wait_cleanup(task: asyncio.Task[None]) -> None:
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if task.done():
                task.result()
                raise


__all__ = ["MacOSObserverEndpointDiscovery", "ObserverEndpoint"]
