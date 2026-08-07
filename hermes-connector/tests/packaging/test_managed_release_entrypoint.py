from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

CONNECTOR_ROOT = Path(__file__).parents[2]
COMMON_PACKAGING = CONNECTOR_ROOT / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

import hermes_managed_release
from hermes_local_release import BuildCommand, ReleaseBuilder
from hermes_managed_release import ManagedReleaseAssembler, ManagedReleaseBuilder
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


def _portable_manifest(filename: str, wheel_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "plugin_id": "hermes-agent-plugin",
        "version": "0.1.0",
        "artifact_filename": filename,
        "wheel_sha256": wheel_sha256,
        "entrypoint": {
            "group": "hermes_agent.plugins",
            "name": "hermes-agent-plugin",
            "value": "hermes_agent_plugin",
        },
        "signature_algorithm": "ed25519",
        "key_id": "vendor-key-1",
        "issued_at": "2026-08-07T00:00:00Z",
        "expires_at": "2026-08-08T00:00:00Z",
        "signature": "A" * 88,
    }


def _portable_inputs(tmp_path: Path):
    wheel = (tmp_path / "hermes_agent_plugin-0.1.0-py3-none-any.whl").resolve()
    wheel.write_bytes(b"plugin")
    digest = hashlib.sha256(b"plugin").hexdigest()
    return SimpleNamespace(
        core=SimpleNamespace(lock=SimpleNamespace(sha256=CORE_LOCK)),
        connector=SimpleNamespace(lock=SimpleNamespace(sha256=CONNECTOR_LOCK)),
        plugin_bundle=SimpleNamespace(path=wheel, sha256=digest),
        signed_plugin_manifest=_portable_manifest(wheel.name, digest),
    )


def test_managed_release_composition_always_injects_pinned_runner(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class FakeBuilder:
        def __init__(
            self,
            *,
            releases_root,
            runner,
            runtime_plan=None,
            service_renderer=None,
        ):
            captured["releases_root"] = releases_root
            captured["runner"] = runner
            captured["runtime_plan"] = runtime_plan
            captured["service_renderer"] = service_renderer

        def build(self, inputs, *, dry_run=False):
            captured["inputs"] = inputs
            captured["dry_run"] = dry_run
            return "built"

    monkeypatch.setattr(hermes_managed_release, "ManagedReleaseBuilder", FakeBuilder)
    inputs = _inputs()
    assembler = ManagedReleaseAssembler(
        releases_root=(tmp_path / "releases").resolve(),
        toolchain=_toolchain(tmp_path),
        wheelhouse=_wheelhouse(tmp_path),
    )

    assert assembler.build(inputs, dry_run=True) == "built"
    assert isinstance(captured["runner"], PinnedToolchainRunner)
    assert captured["releases_root"] == (tmp_path / "releases").resolve()
    assert captured["runtime_plan"] is None
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

    monkeypatch.setattr(hermes_managed_release, "ManagedReleaseBuilder", FakeBuilder)
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

    monkeypatch.setattr(hermes_managed_release, "ManagedReleaseBuilder", FakeBuilder)
    assembler = ManagedReleaseAssembler(
        releases_root=(tmp_path / "releases").resolve(),
        toolchain=_toolchain(tmp_path),
        wheelhouse=_wheelhouse(tmp_path, core_lock="f" * 64),
    )

    with pytest.raises(WheelhouseError, match="lock mismatch"):
        assembler.build(_inputs())


def test_portable_plugin_v2_requires_external_crypto_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeBuilder:
        def __init__(self, **_kwargs):
            pass

        def build(self, *_args, **_kwargs):
            raise AssertionError("builder must not run without v2 trust proof")

    monkeypatch.setattr(hermes_managed_release, "ManagedReleaseBuilder", FakeBuilder)
    assembler = ManagedReleaseAssembler(
        releases_root=(tmp_path / "releases").resolve(),
        toolchain=_toolchain(tmp_path),
        wheelhouse=_wheelhouse(tmp_path),
    )
    with pytest.raises(RuntimeError, match="requires external cryptographic verification"):
        assembler.build(_portable_inputs(tmp_path), dry_run=True)


def test_portable_plugin_v2_invokes_crypto_verifier_before_builder(
    tmp_path: Path, monkeypatch
) -> None:
    verified: list[object] = []

    class FakeBuilder:
        def __init__(self, **_kwargs):
            pass

        def build(self, inputs, *, dry_run=False):
            assert verified == [inputs]
            return "portable-built"

    monkeypatch.setattr(hermes_managed_release, "ManagedReleaseBuilder", FakeBuilder)
    inputs = _portable_inputs(tmp_path)
    assembler = ManagedReleaseAssembler(
        releases_root=(tmp_path / "releases").resolve(),
        toolchain=_toolchain(tmp_path),
        wheelhouse=_wheelhouse(tmp_path),
        portable_plugin_verifier=lambda candidate: verified.append(candidate),
    )
    assert assembler.build(inputs, dry_run=True) == "portable-built"


def test_portable_plugin_v2_rejects_absolute_path_fields(
    tmp_path: Path, monkeypatch
) -> None:
    inputs = _portable_inputs(tmp_path)
    inputs.signed_plugin_manifest["wheel_path"] = "/Users/example/Hermes/plugin.whl"
    builder = ManagedReleaseBuilder(
        releases_root=(tmp_path / "releases").resolve(),
        runner=SimpleNamespace(run=lambda _command: None),
        runtime_plan=None,
    )
    monkeypatch.setattr(ReleaseBuilder, "_validate_inputs", lambda _self, _inputs: None)
    with pytest.raises(RuntimeError, match="does not match schema v2"):
        builder._validate_inputs(inputs)


def test_managed_release_sync_commands_exclude_default_dependency_groups(
    tmp_path: Path, monkeypatch
) -> None:
    base_commands = (
        BuildCommand(
            purpose="sync-host-dependencies",
            argv=("uv", "sync", "--locked", "--no-install-project"),
            cwd=tmp_path,
            environment=MappingProxyType({"UV_OFFLINE": "1"}),
            release_dir=tmp_path,
        ),
        BuildCommand(
            purpose="verify-host-runtime",
            argv=(str(tmp_path / "python"), "-I", "-c", "pass"),
            cwd=tmp_path,
            environment=MappingProxyType({}),
            release_dir=tmp_path,
        ),
        BuildCommand(
            purpose="sync-connector-dependencies",
            argv=("uv", "sync", "--locked", "--no-install-project"),
            cwd=tmp_path,
            environment=MappingProxyType({"UV_OFFLINE": "1"}),
            release_dir=tmp_path,
        ),
    )
    monkeypatch.setattr(
        ReleaseBuilder,
        "_commands",
        staticmethod(lambda _inputs, _release_dir: base_commands),
    )
    builder = ManagedReleaseBuilder(
        releases_root=(tmp_path / "releases").resolve(),
        runner=SimpleNamespace(run=lambda _command: None),
        runtime_plan=None,
    )

    hardened = builder._commands(SimpleNamespace(), tmp_path)
    sync = [command for command in hardened if command.argv[:2] == ("uv", "sync")]
    assert len(sync) == 2
    assert all(command.argv.count("--no-default-groups") == 1 for command in sync)
    assert "--no-default-groups" not in hardened[1].argv


def test_production_code_cannot_bypass_managed_release_composition() -> None:
    """Customer-runtime code must never instantiate the PATH-capable layout engine directly."""

    allowed = {
        (COMMON_PACKAGING / "hermes_local_release.py").resolve(),
        (COMMON_PACKAGING / "hermes_managed_release.py").resolve(),
    }
    violations: list[str] = []
    for path in CONNECTOR_ROOT.rglob("*.py"):
        resolved = path.resolve()
        if resolved in allowed or "tests" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "ReleaseBuilder(" in source:
            violations.append(str(path.relative_to(CONNECTOR_ROOT)))

    assert violations == [], (
        "production code must assemble customer runtimes through ManagedReleaseAssembler; "
        f"direct ReleaseBuilder use found in: {violations}"
    )
