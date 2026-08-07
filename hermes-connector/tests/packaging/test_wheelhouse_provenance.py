from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

from hermes_wheelhouse_provenance import (
    WheelhouseProvenanceError,
    verify_wheelhouse_direct_urls,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    wheelhouse = (tmp_path / "wheelhouse").resolve()
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo_pkg-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    manifest = {
        "schema_version": 1,
        "platform": "linux",
        "architecture": "x86_64",
        "python_tag": "cp313",
        "locks": {"core": "1" * 64, "connector": "2" * 64},
        "artifacts": [
            {
                "filename": wheel.name,
                "sha256": _sha(wheel.read_bytes()),
                "size_bytes": wheel.stat().st_size,
            }
        ],
    }
    manifest_path = wheelhouse / "WHEELHOUSE-MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    venv = (tmp_path / "venv").resolve()
    metadata = venv / "lib/python3.13/site-packages/demo_pkg-1.0.0.dist-info/direct_url.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "url": wheel.as_uri(),
                "archive_info": {"hash": f"sha256={_sha(wheel.read_bytes())}"},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return wheelhouse, venv, metadata, _sha(manifest_path.read_bytes())


def test_local_declared_wheel_direct_url_is_accepted(tmp_path: Path) -> None:
    wheelhouse, venv, metadata, manifest_sha = _fixture(tmp_path)

    verify_wheelhouse_direct_urls(
        [str(metadata)],
        venv_root=venv,
        wheelhouse_root=wheelhouse,
        expected_manifest_sha256=manifest_sha,
    )


def test_network_direct_url_is_rejected(tmp_path: Path) -> None:
    wheelhouse, venv, metadata, manifest_sha = _fixture(tmp_path)
    metadata.write_text(
        json.dumps({"url": "https://example.invalid/demo.whl", "archive_info": {}}),
        encoding="utf-8",
    )

    with pytest.raises(WheelhouseProvenanceError, match="not a local wheel"):
        verify_wheelhouse_direct_urls(
            [str(metadata)],
            venv_root=venv,
            wheelhouse_root=wheelhouse,
            expected_manifest_sha256=manifest_sha,
        )


def test_local_wheel_outside_verified_root_is_rejected(tmp_path: Path) -> None:
    wheelhouse, venv, metadata, manifest_sha = _fixture(tmp_path)
    outside = (tmp_path / "outside.whl").resolve()
    outside.write_bytes(b"outside")
    metadata.write_text(
        json.dumps({"url": outside.as_uri(), "archive_info": {}}),
        encoding="utf-8",
    )

    with pytest.raises(WheelhouseProvenanceError, match="escaped verified wheelhouse"):
        verify_wheelhouse_direct_urls(
            [str(metadata)],
            venv_root=venv,
            wheelhouse_root=wheelhouse,
            expected_manifest_sha256=manifest_sha,
        )


def test_declared_wheel_digest_tampering_is_rejected(tmp_path: Path) -> None:
    wheelhouse, venv, metadata, manifest_sha = _fixture(tmp_path)
    wheel = wheelhouse / "demo_pkg-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"tampered-wheel")

    with pytest.raises(WheelhouseProvenanceError, match="digest"):
        verify_wheelhouse_direct_urls(
            [str(metadata)],
            venv_root=venv,
            wheelhouse_root=wheelhouse,
            expected_manifest_sha256=manifest_sha,
        )


def test_direct_url_metadata_outside_venv_is_rejected(tmp_path: Path) -> None:
    wheelhouse, venv, metadata, manifest_sha = _fixture(tmp_path)
    outside_metadata = tmp_path / "direct_url.json"
    outside_metadata.write_bytes(metadata.read_bytes())

    with pytest.raises(WheelhouseProvenanceError, match="escaped isolated venv"):
        verify_wheelhouse_direct_urls(
            [str(outside_metadata.resolve())],
            venv_root=venv,
            wheelhouse_root=wheelhouse,
            expected_manifest_sha256=manifest_sha,
        )
