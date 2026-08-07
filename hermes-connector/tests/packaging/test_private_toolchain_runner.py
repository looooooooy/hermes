from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

from hermes_local_release import BuildCommand
from hermes_private_toolchain import (
    PinnedExecutable,
    PinnedToolchainRunner,
    PrivateToolchainError,
    PrivateToolchainV1,
    ToolchainCommandResult,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def run(self, argv, *, cwd, environment):
        self.calls.append((tuple(argv), Path(cwd), dict(environment)))
        return ToolchainCommandResult(stdout="ok")


def _executable(tmp_path: Path, name: str, content: bytes) -> PinnedExecutable:
    path = (tmp_path / name).resolve()
    path.write_bytes(content)
    path.chmod(0o700)
    return PinnedExecutable(
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        version="test-1",
    )


def _runner(tmp_path: Path) -> tuple[PinnedToolchainRunner, RecordingExecutor, PrivateToolchainV1]:
    toolchain = PrivateToolchainV1(
        python=_executable(tmp_path, "hermes-python", b"private-python"),
        uv=_executable(tmp_path, "hermes-uv", b"private-uv"),
    )
    executor = RecordingExecutor()
    return PinnedToolchainRunner(toolchain, executor=executor), executor, toolchain


def _command(tmp_path: Path, argv: tuple[str, ...]) -> BuildCommand:
    return BuildCommand(
        purpose="test",
        argv=argv,
        cwd=tmp_path,
        environment=MappingProxyType(
            {
                "UV_OFFLINE": "1",
                "UV_PROJECT_ENVIRONMENT": str((tmp_path / "venv").resolve()),
            }
        ),
        release_dir=(tmp_path / "release").resolve(),
    )


def test_sync_uses_pinned_uv_and_private_python(tmp_path: Path, monkeypatch) -> None:
    runner, executor, toolchain = _runner(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/host/python")
    monkeypatch.setenv("UV_PYTHON", "/host/python")

    result = runner.run(
        _command(
            tmp_path,
            ("uv", "sync", "--offline", "--project", str(tmp_path), "--locked"),
        )
    )

    assert result.stdout == "ok"
    argv, _, environment = executor.calls[0]
    assert argv[0] == str(toolchain.uv.path)
    assert argv[1:4] == ("sync", "--python", str(toolchain.python.path))
    assert environment["UV_PYTHON"] == str(toolchain.python.path)
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_NO_SYSTEM_CONFIG"] == "1"
    assert "PYTHONPATH" not in environment


def test_uv_pip_requires_explicit_absolute_target_python(tmp_path: Path) -> None:
    runner, _, _ = _runner(tmp_path)

    with pytest.raises(PrivateToolchainError, match="explicit Python"):
        runner.run(_command(tmp_path, ("uv", "pip", "install", "package.whl")))

    with pytest.raises(PrivateToolchainError, match="absolute path"):
        runner.run(
            _command(
                tmp_path,
                ("uv", "pip", "install", "--python", "venv/bin/python", "package.whl"),
            )
        )


def test_non_uv_executable_must_already_be_absolute(tmp_path: Path) -> None:
    runner, _, _ = _runner(tmp_path)

    with pytest.raises(PrivateToolchainError, match="must be absolute"):
        runner.run(_command(tmp_path, ("python", "-c", "print('unsafe')")))


def test_toolchain_digest_is_verified_before_execution(tmp_path: Path) -> None:
    python = _executable(tmp_path, "hermes-python", b"private-python")
    uv = _executable(tmp_path, "hermes-uv", b"private-uv")
    broken = PrivateToolchainV1(
        python=python,
        uv=PinnedExecutable(path=uv.path, sha256="0" * 64, version=uv.version),
    )

    with pytest.raises(PrivateToolchainError, match="digest mismatch"):
        PinnedToolchainRunner(broken, executor=RecordingExecutor())
