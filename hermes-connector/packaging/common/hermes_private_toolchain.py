"""Pinned Hermes toolchain execution for blank-machine Managed Runtime builds.

This runner is intentionally independent of shell PATH discovery.  The caller supplies
content-addressed private Python and uv executables, and every uv project sync is bound
to that exact Python interpreter before a command is allowed to execute.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PrivateToolchainError(RuntimeError):
    """A private toolchain or command violates the zero-host-dependency contract."""


@dataclass(frozen=True)
class PinnedExecutable:
    path: Path
    sha256: str
    version: str


@dataclass(frozen=True)
class PrivateToolchainV1:
    python: PinnedExecutable
    uv: PinnedExecutable


@dataclass(frozen=True)
class ToolchainCommandResult:
    stdout: str = ""


class BuildCommandLike(Protocol):
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]


class Executor(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> ToolchainCommandResult: ...


class SubprocessExecutor:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> ToolchainCommandResult:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(environment),
            check=True,
            capture_output=True,
            text=True,
        )
        return ToolchainCommandResult(stdout=completed.stdout)


class PinnedToolchainRunner:
    """Execute local-release commands using only the declared Hermes toolchain."""

    def __init__(
        self,
        toolchain: PrivateToolchainV1,
        *,
        executor: Executor | None = None,
    ) -> None:
        self._toolchain = toolchain
        self._executor = executor or SubprocessExecutor()
        _validate_executable("private Python", toolchain.python)
        _validate_executable("private uv", toolchain.uv)

    def run(self, command: BuildCommandLike) -> ToolchainCommandResult:
        if not command.argv:
            raise PrivateToolchainError("build command has no executable")

        argv = list(command.argv)
        if argv[0] == "uv":
            argv[0] = str(self._toolchain.uv.path)
            _bind_uv_python(argv, self._toolchain.python.path)
        else:
            executable = Path(argv[0])
            if not executable.is_absolute():
                raise PrivateToolchainError(
                    f"non-uv build executable must be absolute: {argv[0]}"
                )

        environment = _sanitized_environment(command.environment)
        environment["UV_OFFLINE"] = "1"
        environment["UV_NO_SYSTEM_CONFIG"] = "1"
        environment["UV_NO_PROGRESS"] = "1"
        environment["UV_PYTHON"] = str(self._toolchain.python.path)

        return self._executor.run(
            tuple(argv),
            cwd=Path(command.cwd),
            environment=MappingProxyType(environment),
        )


def _bind_uv_python(argv: list[str], python: Path) -> None:
    if len(argv) < 2:
        raise PrivateToolchainError("uv command has no subcommand")

    subcommand = argv[1]
    if subcommand == "sync":
        if "--python" in argv:
            index = argv.index("--python")
            if index + 1 >= len(argv) or Path(argv[index + 1]) != python:
                raise PrivateToolchainError(
                    "uv sync attempted to override the pinned private Python"
                )
        else:
            argv[2:2] = ["--python", str(python)]
        return

    if subcommand == "pip":
        if "--python" not in argv:
            raise PrivateToolchainError("uv pip command must target an explicit Python")
        target = argv[argv.index("--python") + 1]
        if not Path(target).is_absolute():
            raise PrivateToolchainError("uv pip target Python must be an absolute path")
        return

    raise PrivateToolchainError(f"unapproved uv subcommand: {subcommand}")


def _sanitized_environment(declared: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
        and not key.startswith("UV_")
    }
    environment.update(declared)
    return environment


def _validate_executable(label: str, executable: PinnedExecutable) -> None:
    path = Path(executable.path)
    if not path.is_absolute():
        raise PrivateToolchainError(f"{label} path must be absolute")
    if not _SHA256.fullmatch(executable.sha256):
        raise PrivateToolchainError(f"{label} sha256 is invalid")
    if not executable.version.strip():
        raise PrivateToolchainError(f"{label} version is empty")
    if path.is_symlink() or not path.is_file():
        raise PrivateToolchainError(f"{label} is missing or symlinked")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise PrivateToolchainError(f"{label} is not a regular file")
    if os.name != "nt" and mode & 0o111 == 0:
        raise PrivateToolchainError(f"{label} is not executable")
    if _sha256(path) != executable.sha256:
        raise PrivateToolchainError(f"{label} digest mismatch")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
