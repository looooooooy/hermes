"""Fail-closed Windows exact-release activation and rollback transaction."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hermes_windows_release import WindowsValidatedRelease, validate_windows_release
from hermes_windows_tasks import WindowsConnectorTask, build_connector_task, render_connector_launcher

_MAX_RECEIPT_BYTES = 65_536


class WindowsActivationError(RuntimeError):
    pass


class WindowsActivationBlocked(WindowsActivationError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseRef:
    release_id: str
    release_digest: str
    release_dir: Path


@dataclass(frozen=True, slots=True)
class ActiveReceipt:
    profile: str
    active: ReleaseRef
    previous: ReleaseRef | None


@dataclass(frozen=True, slots=True)
class PendingReceipt:
    profile: str
    candidate: ReleaseRef
    previous_active: ActiveReceipt | None


@dataclass(frozen=True, slots=True)
class ActivationResult:
    active: ReleaseRef
    previous: ReleaseRef | None
    changed: bool


class ActivationStore(Protocol):
    launcher_path: Path

    def transaction(self) -> AbstractContextManager[None]: ...

    def read(self, name: str) -> bytes | None: ...

    def write(self, name: str, payload: bytes) -> None: ...

    def delete(self, name: str) -> None: ...

    def write_launcher(self, payload: bytes) -> None: ...

    def delete_launcher(self) -> None: ...


class ActivationPlatform(Protocol):
    def end(self, task: WindowsConnectorTask) -> None: ...

    def register(self, task: WindowsConnectorTask) -> None: ...

    def run(self, task: WindowsConnectorTask) -> None: ...

    def delete(self, task: WindowsConnectorTask) -> None: ...

    def healthy(self, task: WindowsConnectorTask, *, timeout_seconds: float) -> bool: ...


class WindowsActivationController:
    def __init__(
        self,
        *,
        profile: str,
        hermes_home: Path,
        config_file: Path,
        store: ActivationStore,
        platform: ActivationPlatform,
        health_timeout_seconds: float = 20.0,
    ) -> None:
        if health_timeout_seconds <= 0:
            raise ValueError("health timeout must be positive")
        self._profile = profile
        self._hermes_home = Path(hermes_home)
        self._config_file = Path(config_file)
        self._store = store
        self._platform = platform
        self._health_timeout_seconds = health_timeout_seconds

    def activate(
        self,
        *,
        release_dir: Path,
        release_id: str,
    ) -> ActivationResult:
        candidate_release = validate_windows_release(
            release_dir=release_dir,
            expected_release_id=release_id,
        )
        candidate = _release_ref(candidate_release)
        with self._store.transaction():
            if self._store.read("blocked.json") is not None:
                raise WindowsActivationBlocked(
                    "Windows activation is blocked pending operator recovery"
                )
            pending = _decode_pending(self._store.read("pending.json"))
            if pending is not None:
                self._rollback_pending(pending)
            current = _decode_active(self._store.read("active.json"))
            if current is not None and current.profile != self._profile:
                raise WindowsActivationError("active profile does not match")
            if current is not None and current.active == candidate:
                task = self._task(candidate)
                if self._platform.healthy(
                    task,
                    timeout_seconds=self._health_timeout_seconds,
                ):
                    return ActivationResult(
                        active=candidate,
                        previous=current.previous,
                        changed=False,
                    )
            pending = PendingReceipt(
                profile=self._profile,
                candidate=candidate,
                previous_active=current,
            )
            self._store.write("pending.json", _encode_pending(pending))
            try:
                self._apply_candidate(candidate, current)
            except BaseException as error:
                try:
                    self._rollback_pending(pending)
                except BaseException as rollback_error:
                    self._write_blocked(pending)
                    raise WindowsActivationBlocked(
                        "Windows activation rollback failed"
                    ) from rollback_error
                raise WindowsActivationError("Windows activation failed") from error
            receipt = ActiveReceipt(
                profile=self._profile,
                active=candidate,
                previous=current.active if current is not None else None,
            )
            self._store.write("active.json", _encode_active(receipt))
            self._store.delete("pending.json")
            self._store.delete("blocked.json")
            return ActivationResult(
                active=candidate,
                previous=receipt.previous,
                changed=True,
            )

    def recover(self) -> ActiveReceipt | None:
        with self._store.transaction():
            if self._store.read("blocked.json") is not None:
                raise WindowsActivationBlocked(
                    "Windows activation is blocked pending operator recovery"
                )
            pending = _decode_pending(self._store.read("pending.json"))
            if pending is not None:
                self._rollback_pending(pending)
            return _decode_active(self._store.read("active.json"))

    def _apply_candidate(
        self,
        candidate: ReleaseRef,
        current: ActiveReceipt | None,
    ) -> None:
        task = self._task(candidate)
        if current is not None:
            self._platform.end(task)
        self._store.write_launcher(render_connector_launcher(task))
        self._platform.register(task)
        self._platform.run(task)
        if not self._platform.healthy(
            task,
            timeout_seconds=self._health_timeout_seconds,
        ):
            raise WindowsActivationError("candidate did not become healthy")

    def _rollback_pending(self, pending: PendingReceipt) -> None:
        if pending.profile != self._profile:
            raise WindowsActivationError("pending profile does not match")
        candidate_task = self._task(pending.candidate)
        self._platform.end(candidate_task)
        previous_active = pending.previous_active
        if previous_active is None:
            self._platform.delete(candidate_task)
            self._store.delete_launcher()
            self._store.delete("active.json")
            self._store.delete("pending.json")
            return
        previous = previous_active.active
        validated = validate_windows_release(
            release_dir=previous.release_dir,
            expected_release_id=previous.release_id,
        )
        validated_ref = _release_ref(validated)
        if validated_ref != previous:
            raise WindowsActivationError("previous release evidence changed")
        previous_task = self._task(previous)
        self._store.write_launcher(render_connector_launcher(previous_task))
        self._platform.register(previous_task)
        self._platform.run(previous_task)
        if not self._platform.healthy(
            previous_task,
            timeout_seconds=self._health_timeout_seconds,
        ):
            raise WindowsActivationError("previous release did not recover")
        self._store.write("active.json", _encode_active(previous_active))
        self._store.delete("pending.json")

    def _write_blocked(self, pending: PendingReceipt) -> None:
        payload = {
            "schema_version": 1,
            "profile": self._profile,
            "reason": "rollback_failed",
            "candidate": _ref_dict(pending.candidate),
            "previous_active": (
                _active_dict(pending.previous_active)
                if pending.previous_active is not None
                else None
            ),
        }
        self._store.write("blocked.json", _json_bytes(payload))

    def _task(self, release: ReleaseRef) -> WindowsConnectorTask:
        task = build_connector_task(
            release_dir=release.release_dir,
            release_id=release.release_id,
            profile=self._profile,
            hermes_home=self._hermes_home,
            config_file=self._config_file,
        )
        if task.launcher != self._store.launcher_path:
            raise WindowsActivationError("activation launcher path does not match store")
        return task


def _release_ref(release: WindowsValidatedRelease) -> ReleaseRef:
    return ReleaseRef(
        release_id=release.release_id,
        release_digest=release.release_digest,
        release_dir=release.release_dir,
    )


def _encode_active(value: ActiveReceipt) -> bytes:
    return _json_bytes(_active_dict(value))


def _active_dict(value: ActiveReceipt) -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile": value.profile,
        "active": _ref_dict(value.active),
        "previous": _ref_dict(value.previous) if value.previous is not None else None,
    }


def _encode_pending(value: PendingReceipt) -> bytes:
    return _json_bytes(
        {
            "schema_version": 1,
            "profile": value.profile,
            "candidate": _ref_dict(value.candidate),
            "previous_active": (
                _active_dict(value.previous_active)
                if value.previous_active is not None
                else None
            ),
        }
    )


def _decode_active(raw: bytes | None) -> ActiveReceipt | None:
    if raw is None:
        return None
    value = _json_object(raw)
    if set(value) != {"schema_version", "profile", "active", "previous"}:
        raise WindowsActivationError("active receipt schema is invalid")
    if value.get("schema_version") != 1 or not isinstance(value.get("profile"), str):
        raise WindowsActivationError("active receipt is invalid")
    return ActiveReceipt(
        profile=str(value["profile"]),
        active=_decode_ref(value["active"]),
        previous=(
            _decode_ref(value["previous"])
            if value.get("previous") is not None
            else None
        ),
    )


def _decode_pending(raw: bytes | None) -> PendingReceipt | None:
    if raw is None:
        return None
    value = _json_object(raw)
    if set(value) != {
        "schema_version",
        "profile",
        "candidate",
        "previous_active",
    }:
        raise WindowsActivationError("pending receipt schema is invalid")
    if value.get("schema_version") != 1 or not isinstance(value.get("profile"), str):
        raise WindowsActivationError("pending receipt is invalid")
    previous = value.get("previous_active")
    previous_active = None
    if previous is not None:
        previous_raw = _json_bytes(previous)
        previous_active = _decode_active(previous_raw)
    return PendingReceipt(
        profile=str(value["profile"]),
        candidate=_decode_ref(value["candidate"]),
        previous_active=previous_active,
    )


def _ref_dict(value: ReleaseRef | None) -> dict[str, object]:
    if value is None:
        raise TypeError("release reference is required")
    return {
        "release_id": value.release_id,
        "release_digest": value.release_digest,
        "release_dir": str(value.release_dir),
    }


def _decode_ref(value: object) -> ReleaseRef:
    if not isinstance(value, dict) or set(value) != {
        "release_id",
        "release_digest",
        "release_dir",
    }:
        raise WindowsActivationError("release reference schema is invalid")
    release_id = value.get("release_id")
    release_digest = value.get("release_digest")
    release_dir = value.get("release_dir")
    if not all(isinstance(item, str) and item for item in (release_id, release_digest, release_dir)):
        raise WindowsActivationError("release reference is invalid")
    path = Path(str(release_dir))
    if not path.is_absolute() or ".." in path.parts:
        raise WindowsActivationError("release reference path is invalid")
    return ReleaseRef(
        release_id=str(release_id),
        release_digest=str(release_digest),
        release_dir=path,
    )


def _json_object(raw: bytes) -> dict[str, object]:
    if not 1 <= len(raw) <= _MAX_RECEIPT_BYTES:
        raise WindowsActivationError("activation receipt is invalid")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        raise WindowsActivationError("activation receipt is invalid") from None
    if not isinstance(value, dict):
        raise WindowsActivationError("activation receipt is invalid")
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


__all__ = [
    "ActivationResult",
    "ActiveReceipt",
    "PendingReceipt",
    "ReleaseRef",
    "WindowsActivationBlocked",
    "WindowsActivationController",
    "WindowsActivationError",
]
