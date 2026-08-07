from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

from hermes_local_release import BuildCommand
from hermes_offline_wheelhouse import load_verified_wheelhouse
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


def _toolchain(tmp_path: Path) -> PrivateToolchainV1:
    return PrivateToolchainV1(
        python=_executable(tmp_path, "hermes-python", b"private-python"),
        uv=_executable(tmp_path, "hermes-uv", b"private-uv"),
    )


def _wheelhouse(tmp_path: Path):
    root = (tmp_path / "wheelhouse").resolve()
    root.mkdir()
    wheel = b"demo-wheel"
    filename = "demo_pkg-1.0.0-py3-none-any.whl"
    (root / filename).write_bytes(wheel)
    manifest = {
        "schema_version": 1,
        "platform": "test",
        "architecture": "test",
        "python_tag": "cp313",
        "locks": {"core": "1" * 64, "connector": "2" * 64},
        "artifacts": [
            {
                "filename": filename,
                "sha256": hashlib.sha256(wheel).hexdigest(),
                "size_bytes": len(wheel),
            }
        ],
    }
    (root / "WHEELHOUSE-MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return load_verified_wheelhouse(root)


def _runner(
    tmp_path: Path, *, with_wheelhouse: bool = False
) -> tuple[PinnedToolchainRunner, RecordingExecutor, PrivateToolchainV1]:
    toolchain = _toolchain(tmp_path)
    executor = RecordingExecutor()
    wheelhouse = _wheelhouse(tmp_path) if with_wheelhouse else None
    return (
        PinnedToolchainRunner(
            toolchain,
            wheelhouse=wheelhouse,
            executor=executor,
        ),
        executor,
        toolchain,
    )


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
    assert environment["UV_NO_CONFIG"] == "1"
    assert environment["UV_NO_PYTHON_DOWNLOADS"] == "1"
    assert "PYTHONPATH" not in environment


def test_verified_wheelhouse_disables_registry_and_binds_find_links(tmp_path: Path) -> None:
    runner, executor, _ = _runner(tmp_path, with_wheelhouse=True)

    runner.run(
        _command(
            tmp_path,
            ("uv", "sync", "--offline", "--project", str(tmp_path), "--locked"),
        )
    )

    argv, _, environment = executor.calls[0]
    assert argv[1] == "sync"
    assert "--no-index" in argv
    assert environment["UV_FIND_LINKS"] == str((tmp_path / "wheelhouse").resolve())
    assert environment["UV_NO_CONFIG"] == "1"
    assert environment["UV_NO_PYTHON_DOWNLOADS"] == "1"


def test_uv_venv_uses_exact_private_python_without_registry_flags(tmp_path: Path) -> None:
    runner, executor, toolchain = _runner(tmp_path, with_wheelhouse=True)
    target = (tmp_path / "managed-venv").resolve()

    runner.run(_command(tmp_path, ("uv", "venv", "--offline", str(target))))

    argv, _, environment = executor.calls[0]
    assert argv[:4] == (
        str(toolchain.uv.path),
        "venv",
        "--python",
        str(toolchain.python.path),
    )
    assert "--no-index" not in argv
    assert argv[-1] == str(target)
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_PYTHON"] == str(toolchain.python.path)


def test_uv_venv_rejects_private_python_override(tmp_path: Path) -> None:
    runner, _, _ = _runner(tmp_path)
    other = (tmp_path / "other-python").resolve()

    with pytest.raises(PrivateToolchainError, match="override"):
        runner.run(
            _command(
                tmp_path,
                ("uv", "venv", "--python", str(other), str(tmp_path / "venv")),
            )
        )


def test_uv_pip_target_plan_is_offline_and_wheelhouse_bound(tmp_path: Path) -> None:
    runner, executor, _ = _runner(tmp_path, with_wheelhouse=True)
    target_python = (tmp_path / "venv" / "bin" / "python").resolve()
    requirements = (tmp_path / "runtime-requirements.txt").resolve()

    runner.run(
        _command(
            tmp_path,
            (
                "uv",
                "pip",
                "install",
                "--offline",
                "--python",
                str(target_python),
                "--require-hashes",
                "--no-deps",
                "--requirement",
                str(requirements),
            ),
        )
    )

    argv, _, environment = executor.calls[0]
    assert argv[0] != "uv"
    assert argv[1:3] == ("pip", "install")
    assert "--no-index" in argv
    assert "--require-hashes" in argv
    assert "--no-deps" in argv
    assert environment["UV_FIND_LINKS"] == str((tmp_path / "wheelhouse").resolve())


def test_uv_pip_requires_explicit_absolute_target_python(tmp_path: Path) -> None:
    runner, _, _ = _runner(tmp_path)

    with pytest.raises(PrivateToolchainError, match="explicit Python"):
        runner.run(_command(tmp_path, ("uv", "pip", "install", "package.whl")))

    with pytest.raises(PrivateToolchainError, match="absolute path"):
        runner.run(
            _command(
                tmp_path,
                (
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    "venv/bin/python",
                    "package.whl",
                ),
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
