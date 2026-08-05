"""Production signed Plugin Store release assembler contract."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER_PATH = PLUGIN_ROOT / "packaging/common/plugin_store_bundle.py"
CORE_VERIFIER_PATH = (
    PLUGIN_ROOT.parent
    / ".tmp/hermes-core-host-spi-v1-slice-a-replay-r1/hermes_cli/plugin_store_v1.py"
)


def _load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _assembler():
    return _load_path("hermes_plugin_store_bundle", ASSEMBLER_PATH)


def _core_verifier():
    return _load_path("hermes_core_plugin_store_v1", CORE_VERIFIER_PATH)


def _private_key(path: Path, *, mode: int = 0o600) -> bytes:
    contents = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(contents)
    path.chmod(mode)
    return contents


def _times() -> dict[str, datetime]:
    now = datetime.now(timezone.utc)
    return {
        "now": now,
        "issued_at": now - timedelta(minutes=1),
        "expires_at": now + timedelta(hours=1),
        "key_not_before": now - timedelta(days=1),
        "key_not_after": now + timedelta(days=30),
    }


def _assemble(
    tmp_path: Path,
    canonical_wheel: Path,
    *,
    key_path: Path | None = None,
    release_root: Path | None = None,
    store_root: Path | None = None,
    **time_overrides,
):
    module = _assembler()
    key = key_path or (tmp_path / "release-signing-key.pem")
    if key_path is None:
        _private_key(key)
    release = release_root or (tmp_path / "release-v1")
    state = store_root or (tmp_path / "state")
    if store_root is None:
        state.mkdir(mode=0o700)
    timing = {**_times(), **time_overrides}
    return module.assemble_signed_plugin_store_bundle(
        wheel_path=canonical_wheel,
        private_key_path=key,
        release_root=release,
        store_root=state,
        key_id="release-key-1",
        **timing,
    )


def test_assembler_outputs_core_exact_signed_bundle_with_external_mutable_state(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    key_path = tmp_path / "release-signing-key.pem"
    private_bytes = _private_key(key_path)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    bundle = _assemble(
        tmp_path,
        canonical_wheel,
        key_path=key_path,
        store_root=state,
    )

    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version",
        "plugin_id",
        "version",
        "wheel_path",
        "wheel_sha256",
        "store_root",
        "entrypoint",
        "signature_algorithm",
        "key_id",
        "issued_at",
        "expires_at",
        "signature",
    }
    assert manifest["store_root"] == str(state)
    assert Path(manifest["wheel_path"]).is_relative_to(bundle.release_root)
    assert bundle.wheel_path.relative_to(bundle.release_root).parts[:3] == (
        "plugin",
        "artifacts",
        "hermes-agent-plugin",
    )
    assert bundle.manifest_path == (
        bundle.release_root / "plugin/metadata/signed-plugin-manifest.json"
    )
    assert bundle.trust_store_path == (
        bundle.release_root / "plugin/metadata/trust-store.json"
    )
    assert Path(manifest["wheel_path"]).stat().st_mode & 0o222 == 0
    assert bundle.manifest_path.stat().st_mode & 0o222 == 0
    assert bundle.trust_store_path.stat().st_mode & 0o222 == 0
    assert bundle.release_root.stat().st_mode & 0o222 == 0
    assert state.stat().st_mode & 0o700 == 0o700
    assert key_path.read_bytes() == private_bytes
    assert all(private_bytes not in path.read_bytes() for path in bundle.release_root.rglob("*") if path.is_file())

    candidate = _core_verifier().prepare_plugin_bundle(
        bundle.manifest_path,
        bundle.trust_store_path,
    )
    assert candidate.store_root == state
    assert candidate.slot_path.is_relative_to(state / "slots")
    assert candidate.slot_path.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o666])
def test_private_key_permissions_are_fail_closed(
    tmp_path: Path,
    canonical_wheel: Path,
    mode: int,
) -> None:
    key = tmp_path / "signing-key.pem"
    _private_key(key, mode=mode)

    with pytest.raises(Exception, match="private|permission"):
        _assemble(tmp_path, canonical_wheel, key_path=key)

    assert not (tmp_path / "release-v1").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_private_key_links_are_rejected_without_publishing(
    tmp_path: Path,
    canonical_wheel: Path,
    link_kind: str,
) -> None:
    key = tmp_path / "real-key.pem"
    _private_key(key)
    linked = tmp_path / "linked-key.pem"
    if link_kind == "symlink":
        linked.symlink_to(key)
    else:
        os.link(key, linked)

    with pytest.raises(Exception, match="symlink|hardlink|link"):
        _assemble(tmp_path, canonical_wheel, key_path=linked)

    assert not (tmp_path / "release-v1").exists()


def test_expired_bundle_is_rejected_before_any_release_side_effect(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    now = datetime.now(timezone.utc)

    with pytest.raises(Exception, match="expired|expires"):
        _assemble(
            tmp_path,
            canonical_wheel,
            now=now,
            issued_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )

    assert not (tmp_path / "release-v1").exists()


@pytest.mark.parametrize("relationship", ["state-in-release", "release-in-state"])
def test_release_and_mutable_state_must_be_disjoint(
    tmp_path: Path,
    canonical_wheel: Path,
    relationship: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    if relationship == "state-in-release":
        release = tmp_path / "release-v1"
        nested_state = release / "state"
        release.mkdir()
        nested_state.mkdir(mode=0o700)
        state = nested_state
    else:
        release = state / "release-v1"

    with pytest.raises(Exception, match="disjoint|state|release"):
        _assemble(
            tmp_path,
            canonical_wheel,
            release_root=release,
            store_root=state,
        )

    assert not (release / "plugin/metadata/signed-plugin-manifest.json").exists()


def test_manifest_tamper_is_rejected_by_core_signature_verifier(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    bundle = _assemble(tmp_path, canonical_wheel)
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "9.9.9"
    bundle.manifest_path.chmod(0o600)
    bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bundle.manifest_path.chmod(0o400)

    with pytest.raises(Exception, match="signature"):
        _core_verifier().prepare_plugin_bundle(
            bundle.manifest_path,
            bundle.trust_store_path,
        )


def test_atomic_publish_failure_removes_partial_release_and_keeps_state_empty(
    tmp_path: Path,
    canonical_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _assembler()
    key = tmp_path / "signing-key.pem"
    _private_key(key)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    release = tmp_path / "release-v1"

    def fail_publish(_source, _target):
        raise OSError("controlled atomic publish failure")

    monkeypatch.setattr(module.os, "replace", fail_publish)
    with pytest.raises(Exception, match="publish|assemble"):
        module.assemble_signed_plugin_store_bundle(
            wheel_path=canonical_wheel,
            private_key_path=key,
            release_root=release,
            store_root=state,
            key_id="release-key-1",
            **_times(),
        )

    assert not release.exists()
    assert not list(tmp_path.glob(".release-v1.partial-*"))
    assert not list(state.iterdir())


def test_private_state_root_requires_canonical_non_symlink_directory(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    real_state = tmp_path / "real-state"
    real_state.mkdir(mode=0o700)
    state_link = tmp_path / "state"
    state_link.symlink_to(real_state, target_is_directory=True)

    with pytest.raises(Exception, match="symlink|canonical"):
        _assemble(tmp_path, canonical_wheel, store_root=state_link)

    assert not (tmp_path / "release-v1").exists()


def test_artifact_slot_is_regular_read_only_file(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    bundle = _assemble(tmp_path, canonical_wheel)
    metadata = bundle.wheel_path.lstat()

    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o222 == 0
