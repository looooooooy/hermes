"""Windows private-state and Scheduled Task adapters for activation."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes_connector.adapters.platform.windows.private_state import (
    atomic_write_private_file,
    delete_private_file,
    ensure_private_directory,
    private_file_exists,
    private_named_mutex,
    read_private_file,
)

from hermes_windows_tasks import (
    WindowsConnectorTask,
    create_task_command,
    delete_task_command,
    end_task_command,
    run_task_command,
)

_MAX_STATE_BYTES = 65_536
_MAX_STATUS_BYTES = 65_536


class WindowsActivationEffectError(RuntimeError):
    pass


class WindowsPrivateActivationStore:
    def __init__(
        self,
        *,
        hermes_home: Path,
        profile: str,
    ) -> None:
        self._root = (
            Path(hermes_home)
            / "connector"
            / "profiles"
            / profile
            / "activation"
        )
        self._mutex_key = f"activation:{self._root}"

    @property
    def launcher_path(self) -> Path:
        return self._root / "run-connector.cmd"

    def provision(self) -> Path:
        missing: list[Path] = []
        current = self._root
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise WindowsActivationEffectError(
                    "activation state has no existing ancestor"
                )
            current = parent
        for directory in reversed(missing):
            ensure_private_directory(directory)
        if not missing:
            ensure_private_directory(self._root)
        return self._root

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.provision()
        with private_named_mutex(self._mutex_key):
            yield

    def read(self, name: str) -> bytes | None:
        path = self._state_path(name)
        if not private_file_exists(path):
            return None
        return read_private_file(path, maximum=_MAX_STATE_BYTES)

    def write(self, name: str, payload: bytes) -> None:
        path = self._state_path(name)
        atomic_write_private_file(path, payload, maximum=_MAX_STATE_BYTES)

    def delete(self, name: str) -> None:
        path = self._state_path(name)
        delete_private_file(path, missing_ok=True)

    def write_launcher(self, payload: bytes) -> None:
        atomic_write_private_file(
            self.launcher_path,
            payload,
            maximum=_MAX_STATE_BYTES,
        )

    def delete_launcher(self) -> None:
        delete_private_file(self.launcher_path, missing_ok=True)

    def _state_path(self, name: str) -> Path:
        if name not in {"active.json", "pending.json", "blocked.json"}:
            raise WindowsActivationEffectError("activation state name is invalid")
        return self._root / name


class SubprocessWindowsActivationPlatform:
    def end(self, task: WindowsConnectorTask) -> None:
        self._run_cleanup(end_task_command(task))

    def register(self, task: WindowsConnectorTask) -> None:
        self._run_effect(create_task_command(task), "task registration")

    def run(self, task: WindowsConnectorTask) -> None:
        self._run_effect(run_task_command(task), "task start")

    def delete(self, task: WindowsConnectorTask) -> None:
        self._run_cleanup(delete_task_command(task))

    def healthy(self, task: WindowsConnectorTask, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        environment = dict(os.environ)
        environment["HERMES_HOME"] = str(task.hermes_home)
        environment["HERMES_CONNECTOR_CONFIG_FILE"] = str(task.config_file)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                completed = subprocess.run(
                    [str(task.executable), "status", "--json"],
                    check=False,
                    capture_output=True,
                    text=False,
                    env=environment,
                    timeout=min(3.0, remaining),
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode == 0:
                raw = completed.stdout
                if 1 <= len(raw) <= _MAX_STATUS_BYTES:
                    try:
                        payload = json.loads(raw.decode("utf-8", errors="strict"))
                    except (UnicodeDecodeError, ValueError):
                        payload = None
                    if (
                        isinstance(payload, dict)
                        and payload.get("ready") is True
                        and payload.get("release_id") == task.release_id
                    ):
                        return True
            time.sleep(min(0.25, max(0.0, remaining)))

    def _run_effect(self, command: tuple[str, ...], effect: str) -> None:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WindowsActivationEffectError(f"{effect} outcome is unknown") from error
        if completed.returncode != 0:
            raise WindowsActivationEffectError(f"{effect} failed")

    def _run_cleanup(self, command: tuple[str, ...]) -> None:
        try:
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WindowsActivationEffectError("task cleanup outcome is unknown") from error


__all__ = [
    "SubprocessWindowsActivationPlatform",
    "WindowsActivationEffectError",
    "WindowsPrivateActivationStore",
]
