"""Coherent macOS process-identity capture and shared conformance vectors."""

from __future__ import annotations

import ctypes
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_connector.adapters.platform.macos import process_identity


class _FakeFunction:
    def __init__(self, implementation) -> None:
        self._implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._implementation(*args)


def _install_fake_libproc(
    monkeypatch,
    *,
    start_times: tuple[tuple[int, int] | None, ...],
    executable_paths: tuple[Path | None, ...],
) -> tuple[list[str], object]:
    observed: list[str] = []
    starts = iter(start_times)
    paths = iter(executable_paths)

    def proc_pidinfo(pid, _flavor, _argument, pointer, size):
        observed.append("info")
        snapshot = next(starts)
        if snapshot is None:
            return 0
        seconds, microseconds = snapshot
        pointer._obj.pbi_pid = pid
        pointer._obj.pbi_start_tvsec = seconds
        pointer._obj.pbi_start_tvusec = microseconds
        return size

    def proc_pidpath(_pid, buffer, _size):
        observed.append("path")
        path = next(paths)
        if path is None:
            return 0
        raw = os.fsencode(path) + b"\x00"
        ctypes.memmove(buffer, raw, len(raw))
        return len(raw)

    library = SimpleNamespace(
        proc_pidinfo=_FakeFunction(proc_pidinfo),
        proc_pidpath=_FakeFunction(proc_pidpath),
    )
    monkeypatch.setattr(
        process_identity.os,
        "uname",
        lambda: SimpleNamespace(sysname="Darwin"),
    )
    monkeypatch.setattr(
        process_identity.ctypes.util,
        "find_library",
        lambda _name: "libproc",
    )
    monkeypatch.setattr(
        process_identity.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: library,
    )
    return observed, library


def test_capture_reads_one_stable_process_and_path_snapshot_twice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "Hermes"
    executable.write_bytes(b"mach-o")
    observed, _library = _install_fake_libproc(
        monkeypatch,
        start_times=((10, 100), (10, 100)),
        executable_paths=(executable, executable),
    )

    identity = process_identity.current_process_identity(4242)

    assert identity is not None
    assert identity.start_time_ns == 10_000_100_000
    assert identity.executable_path == executable
    assert (identity.executable_device, identity.executable_inode) == (
        executable.stat().st_dev,
        executable.stat().st_ino,
    )
    assert observed == ["info", "path", "info", "path"]


@pytest.mark.parametrize(
    ("start_times", "executable_paths"),
    (
        (((10, 100), (11, 100)), None),
        (((10, 100), None), None),
        (((10, 100), (10, 100)), "changed-path"),
    ),
    ids=("pid-reuse", "process-exit", "exec-path-change"),
)
def test_capture_rejects_mixed_process_snapshots(
    tmp_path: Path,
    monkeypatch,
    start_times: tuple[tuple[int, int] | None, ...],
    executable_paths: str | None,
) -> None:
    executable = tmp_path / "Hermes"
    executable.write_bytes(b"original")
    if executable_paths == "changed-path":
        changed = tmp_path / "HermesNext"
        changed.write_bytes(b"next")
        paths = (executable, changed)
    else:
        paths = (executable, executable)
    _install_fake_libproc(
        monkeypatch,
        start_times=start_times,
        executable_paths=paths,
    )

    assert process_identity.current_process_identity(4242) is None


def test_capture_rejects_executable_path_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "Hermes"
    displaced = tmp_path / "Hermes-old"
    executable.write_bytes(b"original")
    _install_fake_libproc(
        monkeypatch,
        start_times=((10, 100), (10, 100)),
        executable_paths=(executable, executable),
    )
    real_fstat = process_identity.os.fstat
    replaced = False

    def replace_after_capture(file_descriptor: int):
        nonlocal replaced
        metadata = real_fstat(file_descriptor)
        if not replaced and stat.S_ISREG(metadata.st_mode):
            executable.rename(displaced)
            executable.write_bytes(b"replacement")
            replaced = True
        return metadata

    with monkeypatch.context() as replacement:
        replacement.setattr(process_identity.os, "fstat", replace_after_capture)
        assert process_identity.current_process_identity(4242) is None
    assert replaced is True


def test_shared_process_identity_conformance_vectors() -> None:
    fixture = (
        Path(__file__).resolve().parents[4]
        / "contracts/fixtures/conformance/runtime-process-identity-v1.json"
    )
    packet = json.loads(fixture.read_text(encoding="utf-8"))

    assert packet["contract"] == "runtime-process-identity-coherence-v1"
    for vector in packet["vectors"]:
        descriptor = process_identity.normalize_process_identity(
            SimpleNamespace(**vector["descriptor"])
        )
        observed = process_identity.normalize_process_identity(
            SimpleNamespace(**vector["observed"])
        )
        assert descriptor is not None, vector["name"]
        assert observed is not None, vector["name"]
        assert (descriptor == observed) is vector["accepted"], vector["name"]
