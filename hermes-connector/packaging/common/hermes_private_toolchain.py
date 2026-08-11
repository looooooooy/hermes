"""Pinned Hermes toolchain execution for blank-machine Managed Runtime builds.

The runner is independent of shell PATH discovery and can be bound to a verified,
closed wheelhouse. Every uv project sync uses the declared private Python, disables
network/index/config discovery, and searches only the verified local wheel set.
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

from hermes_offline_wheelhouse import VerifiedWheelhouseV1

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FAILURE_DIAGNOSTIC = 4096


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
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            diagnostic = _bounded_diagnostic(completed.stderr or completed.stdout)
            raise PrivateToolchainError(
                f"private toolchain command failed with exit {completed.returncode}: {diagnostic}"
            )
        return ToolchainCommandResult(stdout=completed.stdout)


class PinnedToolchainRunner:
    """Execute local-release commands using only declared Hermes release inputs."""

    def __init__(
        self,
        toolchain: PrivateToolchainV1,
        *,
        wheelhouse: VerifiedWheelhouseV1 | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._toolchain = toolchain
        self._wheelhouse = wheelhouse
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
            if self._wheelhouse is not None:
                _bind_wheelhouse(argv, self._wheelhouse.root)
        else:
            executable = Path(argv[0])
            if not executable.is_absolute():
                raise PrivateToolchainError(
                    f"non-uv build executable must be absolute: {argv[0]}"
                )

        environment = _sanitized_environment(command.environment)
        environment["UV_OFFLINE"] = "1"
        environment["UV_NO_CONFIG"] = "1"
        environment["UV_NO_PYTHON_DOWNLOADS"] = "1"
        environment["UV_NO_PROGRESS"] = "1"
        environment["UV_PYTHON"] = str(self._toolchain.python.path)
        if self._wheelhouse is not None:
            environment["UV_FIND_LINKS"] = str(self._wheelhouse.root)

        return self._executor.run(
            tuple(argv),
            cwd=Path(command.cwd),
            environment=MappingProxyType(environment),
        )


def _bounded_diagnostic(value: str) -> str:
    # Toolchain commands run without shell interpolation and without credentials.
    # Still keep failure material bounded and single-line so CI cannot accidentally
    # turn a resolver diagnostic into an unbounded log surface.
    normalized = " ".join(value.replace("\x00", "").split())
    if not normalized:
        return "no diagnostic output"
    if len(normalized) > _MAX_FAILURE_DIAGNOSTIC:
        normalized = normalized[-_MAX_FAILURE_DIAGNOSTIC:]
    return normalized


def _bind_uv_python(argv: list[str], python: Path) -> None:
    if len(argv) < 2:
        raise PrivateToolchainError("uv command has no subcommand")

    subcommand = argv[1]
    if subcommand in {"sync", "venv"}:
        if "--python" in argv:
            index = argv.index("--python")
            if index + 1 >= len(argv) or Path(argv[index + 1]) != python:
                raise PrivateToolchainError(
                    f"uv {subcommand} attempted to override the pinned private Python"
                )
        else:
            argv[2:2] = ["--python", str(python)]
        if subcommand == "venv" and not Path(argv[-1]).is_absolute():
            raise PrivateToolchainError("uv venv target must be an absolute path")
        return

    if subcommand == "export":
        if "--frozen" not in argv:
            raise PrivateToolchainError("uv export must consume the frozen lock")
        if "--output-file" not in argv:
            raise PrivateToolchainError("uv export must target an explicit output file")
        output_index = argv.index("--output-file") + 1
        if output_index >= len(argv) or not Path(argv[output_index]).is_absolute():
            raise PrivateToolchainError("uv export output must be an absolute path")
        return

    if subcommand == "pip":
        if len(argv) < 3 or argv[2] != "install":
            raise PrivateToolchainError("only uv pip install is approved")
        if "--python" not in argv:
            raise PrivateToolchainError("uv pip command must target an explicit Python")
        target_index = argv.index("--python") + 1
        if target_index >= len(argv) or not Path(argv[target_index]).is_absolute():
            raise PrivateToolchainError("uv pip target Python must be an absolute path")
        return

    raise PrivateToolchainError(f"unapproved uv subcommand: {subcommand}")


def _bind_wheelhouse(argv: list[str], root: Path) -> None:
    if argv[1] not in {"sync", "pip"}:
        return
    if "--find-links" in argv:
        index = argv.index("--find-links") + 1
        if index >= len(argv) or Path(argv[index]) != root:
            raise PrivateToolchainError("uv command attempted to override the wheelhouse")
    insertion = 3 if argv[1] == "pip" else 2
    options: list[str] = []
    if "--no-index" not in argv:
        options.append("--no-index")
    if "--find-links" not in argv:
        options.extend(("--find-links", str(root)))
    argv[insertion:insertion] = options


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
