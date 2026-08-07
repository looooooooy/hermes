from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
sys.path.insert(0, str(COMMON_PACKAGING))

from hermes_target_runtime_plan import (
    TargetRuntimePlanError,
    load_verified_target_runtime_plan,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _plan_root(tmp_path: Path) -> tuple[Path, str]:
    root = (tmp_path / "wheelhouse").resolve()
    root.mkdir()
    wheelhouse = b'{"schema_version":1}'
    (root / "WHEELHOUSE-MANIFEST.json").write_bytes(wheelhouse)
    manifest_sha = _sha(wheelhouse)
    core = b"demo==1.0.0 --hash=sha256:" + b"1" * 64 + b"\n"
    connector = b"other==2.0.0 --hash=sha256:" + b"2" * 64 + b"\n"
    (root / "CORE-RUNTIME-REQUIREMENTS.txt").write_bytes(core)
    (root / "CONNECTOR-RUNTIME-REQUIREMENTS.txt").write_bytes(connector)
    plan = {
        "schema_version": 1,
        "target": "linux-x86_64",
        "platform": "linux",
        "architecture": "x86_64",
        "python_tag": "cp313",
        "wheelhouse_manifest_sha256": manifest_sha,
        "locks": {"core": "a" * 64, "connector": "b" * 64},
        "requirements": {
            "core": {
                "filename": "CORE-RUNTIME-REQUIREMENTS.txt",
                "sha256": _sha(core),
                "size_bytes": len(core),
            },
            "connector": {
                "filename": "CONNECTOR-RUNTIME-REQUIREMENTS.txt",
                "sha256": _sha(connector),
                "size_bytes": len(connector),
            },
        },
    }
    (root / "RUNTIME-INSTALL-PLAN.json").write_text(
        json.dumps(plan, sort_keys=True), encoding="utf-8"
    )
    return root, manifest_sha


def test_target_plan_binds_lock_wheelhouse_and_requirements(tmp_path: Path) -> None:
    root, manifest_sha = _plan_root(tmp_path)

    verified = load_verified_target_runtime_plan(
        root, expected_wheelhouse_manifest_sha256=manifest_sha
    )

    assert verified.target == "linux-x86_64"
    assert verified.platform == "linux"
    assert verified.requirement("core").path.name == "CORE-RUNTIME-REQUIREMENTS.txt"
    assert verified.requirement("connector").path.name == "CONNECTOR-RUNTIME-REQUIREMENTS.txt"
    verified.require_lock("core", "a" * 64)
    verified.require_lock("connector", "b" * 64)


def test_target_plan_rejects_requirement_tampering(tmp_path: Path) -> None:
    root, manifest_sha = _plan_root(tmp_path)
    (root / "CORE-RUNTIME-REQUIREMENTS.txt").write_bytes(b"tampered")

    with pytest.raises(TargetRuntimePlanError, match="integrity"):
        load_verified_target_runtime_plan(
            root, expected_wheelhouse_manifest_sha256=manifest_sha
        )


def test_target_plan_rejects_wrong_wheelhouse_binding(tmp_path: Path) -> None:
    root, _ = _plan_root(tmp_path)

    with pytest.raises(TargetRuntimePlanError, match="binding"):
        load_verified_target_runtime_plan(
            root, expected_wheelhouse_manifest_sha256="f" * 64
        )


def test_target_plan_rejects_wrong_lock(tmp_path: Path) -> None:
    root, manifest_sha = _plan_root(tmp_path)
    verified = load_verified_target_runtime_plan(
        root, expected_wheelhouse_manifest_sha256=manifest_sha
    )

    with pytest.raises(TargetRuntimePlanError, match="lock mismatch"):
        verified.require_lock("core", "0" * 64)
