from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

import hermes_managed_release
from hermes_managed_release import ManagedReleaseAssembler
from hermes_offline_wheelhouse import WheelhouseError, load_verified_wheelhouse
from hermes_private_toolchain import (
    PinnedExecutable,
    PinnedToolchainRunner,
    PrivateToolchainError,
    PrivateToolchainV1,
)

CORE_LOCK = "1" * 64
CONNECTOR_LOCK = "2" * 64


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
        python=_executable(tmp_path, "private-python", b"python"),
        uv=_executable(tmp_path, "private-uv", b"uv"),
    )


def _wheelhouse(tmp_path: Path, *, core_lock: str = CORE_LOCK):
    root = (tmp_path / "wheelhouse").resolve()
    root.mkdir(exist_ok=True)
    wheel = b"managed dependency"
    filename = "managed_dep-1.0.0-py3-none-any.whl"
    (root / filename).write_bytes(wheel)
    manifest = {
        "schema_version": 1,
        "platform": "test",
        "architecture": "test",
        "python_tag": "cp313",
        "locks": {"core": core_lock, "connector": CONNECTOR_LOCK},
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


def _inputs():
    return SimpleNamespace(
        core=SimpleNamespace(lock=SimpleNamespace(sha256=CORE_LOCK)),
        connector=SimpleNamespace(lock=SimpleNamespace(sha256=CONNECTOR_LOCK)),
    )


def test_managed_release_composition_always_injects_pinned_runner(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class FakeBuilder:
        def __init__(self, *, releases_root, runner, service_renderer=None):
            captured["releases_root"] = releases_root
            captured["runner"] = runner
            captured["service_renderer"] = service_renderer

        def build(self, inputs, *, dry_run=False):
            captured["inputs"] = inputs
            captured["dry_run"] = dry_run
            return "built"

    monkeypatch.setattr(hermes_managed_release, "ReleaseBuilder", FakeBuilder)
    inputs = _inputs()
    assembler = ManagedReleaseAssembler(
        releases_root=(tmp_path / "releases").resolve(),
        toolchain=_toolchain(tmp_path),
        wheelhouse=_wheelhouse(tmp_path),
    )

    assert assembler.build(inputs, dry_run=True) == "built"
    assert isinstance(captured["runner"], PinnedToolchainRunner)
    assert captured["releases_root"] == (tmp_path / "releases").resolve()
    assert captured["inputs"] is inputs
    assert captured["dry_run"] is True


def test_managed_release_refuses_unverified_toolchain_before_builder_creation(
    tmp_path: Path, monkeypatch
) -> None:
    builder_created = False

    class FakeBuilder:
        def __init__(self, **_kwargs):
            nonlocal builder_created
            builder_created = True

    monkeypatch.setattr(hermes_managed_release, "ReleaseBuilder", FakeBuilder)
    python = _executable(tmp_path, "private-python", b"python")
    uv = _executable(tmp_path, "private-uv", b"uv")
    bad_toolchain = PrivateToolchainV1(
        python=python,
        uv=PinnedExecutable(path=uv.path, sha256="0" * 64, version=uv.version),
    )

    with pytest.raises(PrivateToolchainError, match="digest mismatch"):
        ManagedReleaseAssembler(
            releases_root=(tmp_path / "releases").resolve(),
            toolchain=bad_toolchain,
            wheelhouse=_wheelhouse(tmp_path),
        )

    assert builder_created is False


def test_managed_release_rejects_wheelhouse_for_different_lock(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeBuilder:
        def __init__(self, **_kwargs):
            pass

        def build(self, *_args, **_kwargs):
            raise AssertionError("builder must not run after lock mismatch")

    monkeypatch.setattr(hermes_managed_release, "ReleaseBuilder", FakeBuilder)
    assembler = ManagedReleaseAssembler(
        releases_root=(tmp_path / "releases").resolve(),
        toolchain=_toolchain(tmp_path),
        wheelhouse=_wheelhouse(tmp_path, core_lock="f" * 64),
    )

    with pytest.raises(WheelhouseError, match="lock mismatch"):
        assembler.build(_inputs())
