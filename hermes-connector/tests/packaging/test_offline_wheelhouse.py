from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

from hermes_offline_wheelhouse import WheelhouseError, load_verified_wheelhouse


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _wheelhouse(tmp_path: Path) -> tuple[Path, dict[str, object], bytes]:
    root = (tmp_path / "wheelhouse").resolve()
    root.mkdir()
    wheel = b"hermes dependency wheel"
    (root / "demo_pkg-1.0.0-py3-none-any.whl").write_bytes(wheel)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "platform": "macos",
        "architecture": "arm64",
        "python_tag": "cp313",
        "locks": {"core": "1" * 64, "connector": "2" * 64},
        "artifacts": [
            {
                "filename": "demo_pkg-1.0.0-py3-none-any.whl",
                "sha256": _sha(wheel),
                "size_bytes": len(wheel),
            }
        ],
    }
    (root / "WHEELHOUSE-MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return root, manifest, wheel


def test_verified_wheelhouse_binds_both_runtime_locks(tmp_path: Path) -> None:
    root, _, _ = _wheelhouse(tmp_path)

    verified = load_verified_wheelhouse(root)

    verified.require_lock("core", "1" * 64)
    verified.require_lock("connector", "2" * 64)
    assert verified.root == root
    assert len(verified.manifest.artifacts) == 1
    assert len(verified.manifest_sha256) == 64


def test_undeclared_extra_wheel_fails_closed(tmp_path: Path) -> None:
    root, _, _ = _wheelhouse(tmp_path)
    (root / "surprise_pkg-9.9.9-py3-none-any.whl").write_bytes(b"unexpected")

    with pytest.raises(WheelhouseError, match="undeclared"):
        load_verified_wheelhouse(root)


def test_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    root, manifest, wheel = _wheelhouse(tmp_path)
    artifact = manifest["artifacts"][0]
    assert isinstance(artifact, dict)
    artifact["sha256"] = "0" * 64
    (root / "WHEELHOUSE-MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(WheelhouseError, match="digest mismatch"):
        load_verified_wheelhouse(root)


def test_path_like_artifact_name_is_rejected(tmp_path: Path) -> None:
    root, manifest, wheel = _wheelhouse(tmp_path)
    artifact = manifest["artifacts"][0]
    assert isinstance(artifact, dict)
    artifact["filename"] = "../escape.whl"
    artifact["sha256"] = _sha(wheel)
    (root / "WHEELHOUSE-MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(WheelhouseError, match="filename"):
        load_verified_wheelhouse(root)


def test_wrong_lock_digest_is_rejected_at_runtime_binding(tmp_path: Path) -> None:
    root, _, _ = _wheelhouse(tmp_path)
    verified = load_verified_wheelhouse(root)

    with pytest.raises(WheelhouseError, match="lock mismatch"):
        verified.require_lock("core", "f" * 64)
