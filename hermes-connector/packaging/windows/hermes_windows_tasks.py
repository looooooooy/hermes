"""Exact-release per-user Scheduled Task contract for Hermes Connector."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_TASK_PREFIX = "Hermes Connector"


class WindowsTaskContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WindowsConnectorTask:
    release_dir: Path
    release_id: str
    profile: str
    hermes_home: Path
    config_file: Path
    executable: Path
    launcher: Path

    @property
    def task_name(self) -> str:
        return f"{_TASK_PREFIX} [{self.profile}]"

    @property
    def task_action(self) -> str:
        launcher = _windows_text(self.launcher)
        return f'cmd.exe /d /s /c ""{launcher}""'


def build_connector_task(
    *,
    release_dir: Path,
    release_id: str,
    profile: str,
    hermes_home: Path,
    config_file: Path,
) -> WindowsConnectorTask:
    release_dir = _absolute(release_dir, "release directory")
    hermes_home = _absolute(hermes_home, "HERMES_HOME")
    config_file = _absolute(config_file, "configuration file")
    if _RELEASE.fullmatch(release_id) is None or release_dir.name != release_id:
        raise WindowsTaskContractError("release identity is invalid")
    if _PROFILE.fullmatch(profile) is None:
        raise WindowsTaskContractError("profile is invalid")
    if not _is_within(config_file, hermes_home):
        raise WindowsTaskContractError("configuration file must be inside HERMES_HOME")
    executable = release_dir / "connector" / "hermes-connector.exe"
    launcher = (
        hermes_home
        / "connector"
        / "profiles"
        / profile
        / "activation"
        / "run-connector.cmd"
    )
    return WindowsConnectorTask(
        release_dir=release_dir,
        release_id=release_id,
        profile=profile,
        hermes_home=hermes_home,
        config_file=config_file,
        executable=executable,
        launcher=launcher,
    )


def render_connector_launcher(task: WindowsConnectorTask) -> bytes:
    _validate_task(task)
    lines = (
        "@echo off",
        "setlocal",
        f'set "HERMES_HOME={_windows_text(task.hermes_home)}"',
        f'set "HERMES_CONNECTOR_CONFIG_FILE={_windows_text(task.config_file)}"',
        (
            f'"{_windows_text(task.executable)}" run '
            f'--release-id "{task.release_id}"'
        ),
        "exit /b %ERRORLEVEL%",
        "",
    )
    return "\r\n".join(lines).encode("utf-8")


def create_task_command(task: WindowsConnectorTask) -> tuple[str, ...]:
    _validate_task(task)
    return (
        "schtasks.exe",
        "/Create",
        "/F",
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TN",
        task.task_name,
        "/TR",
        task.task_action,
    )


def run_task_command(task: WindowsConnectorTask) -> tuple[str, ...]:
    _validate_task(task)
    return ("schtasks.exe", "/Run", "/TN", task.task_name)


def end_task_command(task: WindowsConnectorTask) -> tuple[str, ...]:
    _validate_task(task)
    return ("schtasks.exe", "/End", "/TN", task.task_name)


def delete_task_command(task: WindowsConnectorTask) -> tuple[str, ...]:
    _validate_task(task)
    return ("schtasks.exe", "/Delete", "/F", "/TN", task.task_name)


def query_task_command(task: WindowsConnectorTask) -> tuple[str, ...]:
    _validate_task(task)
    return ("schtasks.exe", "/Query", "/TN", task.task_name, "/XML")


def _validate_task(task: WindowsConnectorTask) -> None:
    if task.release_dir.name != task.release_id:
        raise WindowsTaskContractError("task release identity drifted")
    if not _is_within(task.executable, task.release_dir):
        raise WindowsTaskContractError("connector executable escaped release")
    expected_launcher = (
        task.hermes_home
        / "connector"
        / "profiles"
        / task.profile
        / "activation"
        / "run-connector.cmd"
    )
    if task.launcher != expected_launcher or not _is_within(
        task.launcher,
        task.hermes_home,
    ):
        raise WindowsTaskContractError("launcher escaped profile activation state")
    if not _is_within(task.config_file, task.hermes_home):
        raise WindowsTaskContractError("configuration escaped HERMES_HOME")
    if task.executable.name.lower() != "hermes-connector.exe":
        raise WindowsTaskContractError("connector executable name is invalid")
    if task.launcher.name.lower() != "run-connector.cmd":
        raise WindowsTaskContractError("launcher name is invalid")


def _absolute(path: Path, name: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise WindowsTaskContractError(f"{name} must be absolute and canonical")
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        Path(path).relative_to(parent)
    except ValueError:
        return False
    return True


def _windows_text(path: Path) -> str:
    return str(PureWindowsPath(path))


__all__ = [
    "WindowsConnectorTask",
    "WindowsTaskContractError",
    "build_connector_task",
    "create_task_command",
    "delete_task_command",
    "end_task_command",
    "query_task_command",
    "render_connector_launcher",
    "run_task_command",
]
