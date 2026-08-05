from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIST_DIRECTORY = PROJECT_ROOT / "dist"

CRITICAL_SOURCE_MODULES = (
    "src/hermes_connector/domain/local_gateway.py",
    "src/hermes_connector/application/local_gateway_client.py",
    "src/hermes_connector/application/supervisor.py",
    "src/hermes_connector/adapters/platform/macos/agent_discovery.py",
    "src/hermes_connector/adapters/platform/macos/observer_discovery.py",
    "src/hermes_connector/adapters/platform/macos/local_gateway_transport.py",
    "src/hermes_connector/adapters/platform/macos/observer_client.py",
    "src/hermes_connector/adapters/platform/macos/plugin_control_relay.py",
    "src/hermes_connector/bootstrap/macos.py",
    "src/hermes_connector/cli.py",
)
NEW_SAFETY_MODULES = (
    "src/hermes_connector/adapters/platform/macos/process_identity.py",
    "src/hermes_connector/adapters/platform/macos/local_runtime_preflight.py",
    "src/hermes_connector/contracts/__init__.py",
    "src/hermes_connector/contracts/mobile_control.py",
    "src/hermes_connector/contracts/generated/sources/mobile-control-v1.json",
    "src/hermes_connector/contracts/generated/__init__.py",
    "src/hermes_connector/contracts/observer_v2.py",
    "src/hermes_connector/application/observer_projection_v2.py",
    "src/hermes_connector/domain/session_catalog.py",
    "src/hermes_connector/application/session_catalog_outbound_lane.py",
    "src/hermes_connector/application/session_catalog_sync.py",
    "src/hermes_connector/adapters/platform/macos/session_catalog_client.py",
    "src/hermes_connector/adapters/persistence/sqlite/models/session_catalog_outbox.py",
    "src/hermes_connector/adapters/persistence/sqlite/models/session_catalog_ack_receipt.py",
    "src/hermes_connector/adapters/persistence/sqlite/repositories/session_catalog_outbox.py",
    "src/hermes_connector/adapters/persistence/sqlite/migrations/session_catalog_v8.py",
    "src/hermes_connector/adapters/persistence/sqlite/migrations/session_catalog_ack_receipt_v9.py",
    "src/hermes_connector/contracts/generated/observer-output-parity-v2.json",
    "src/hermes_connector/contracts/generated/schemas/cloud/payloads/session-snapshot-v2.schema.json",
    "src/hermes_connector/contracts/generated/schemas/cloud/payloads/session-event-v2.schema.json",
    "src/hermes_connector/contracts/generated/schemas/cloud/payloads/session-observe-open-v2.schema.json",
    "src/hermes_connector/contracts/generated/schemas/cloud/payloads/session-observe-close-v2.schema.json",
    "src/hermes_connector/contracts/generated/schemas/cloud/payloads/stream-ack-v2.schema.json",
    "src/hermes_connector/contracts/generated/schemas/cloud/payloads/stream-nack-v2.schema.json",
)


def test_real_wheel_and_sdist_match_all_critical_source_modules() -> None:
    for artifact in _required_artifacts():
        members = _artifact_members(artifact)
        for source_name in CRITICAL_SOURCE_MODULES:
            assert (
                _required_member(members, source_name)
                == (PROJECT_ROOT / source_name).read_bytes()
            ), f"{artifact.name} contains stale {source_name}"


def test_real_wheel_and_sdist_contain_current_new_safety_modules() -> None:
    for artifact in _required_artifacts():
        members = _artifact_members(artifact)
        for source_name in NEW_SAFETY_MODULES:
            assert (
                _required_member(members, source_name)
                == (PROJECT_ROOT / source_name).read_bytes()
            ), f"{artifact.name} is missing or has stale {source_name}"


def test_real_wheel_and_sdist_exclude_test_harnesses() -> None:
    for artifact in _required_artifacts():
        forbidden = [
            member for member in _artifact_members(artifact) if _is_test_harness(member)
        ]
        assert forbidden == [], f"{artifact.name} contains test harnesses: {forbidden}"


@pytest.mark.parametrize("artifact_kind", ("wheel", "sdist"))
def test_real_dist_agent_parser_rejects_descriptor_v1(
    artifact_kind: str,
    tmp_path: Path,
) -> None:
    artifacts = _required_artifacts()
    artifact = next(
        item
        for item in artifacts
        if (item.suffix == ".whl") == (artifact_kind == "wheel")
    )
    import_root = _artifact_import_root(artifact, tmp_path)
    script = """
import json
from hermes_connector.adapters.platform.macos import agent_discovery
from hermes_connector.contracts.mobile_control import CONTROL_ERROR_CODES

descriptor = {
    "version": 1,
    "pid": 123,
    "profile": "default",
    "socket_path": "/tmp/hermes-local-gateway-501/gateway.sock",
    "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
}
print(json.dumps({
    "origin": agent_discovery.__file__,
    "control_error_count": len(CONTROL_ERROR_CODES),
    "rejected": agent_discovery._parse_descriptor(
        descriptor,
        expected_profile="default",
    ) is None,
}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.fspath(import_root)
    environment["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["control_error_count"] == 18
    assert report["rejected"] is True
    assert os.fspath(import_root) in report["origin"]


@pytest.mark.parametrize("artifact_kind", ("wheel", "sdist"))
def test_real_dist_loads_exact_observer_v2_generated_family(
    artifact_kind: str,
    tmp_path: Path,
) -> None:
    artifact = next(
        item
        for item in _required_artifacts()
        if (item.suffix == ".whl") == (artifact_kind == "wheel")
    )
    import_root = _artifact_import_root(artifact, tmp_path)
    script = """
import json
from hermes_connector.contracts.observer_v2 import load_observer_v2_contracts

contracts = load_observer_v2_contracts()
print(json.dumps({
    "capability": contracts.capability,
    "schema_types": sorted(contracts.schemas),
}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.fspath(import_root)
    environment["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "capability": "session.observe.output-parity.v1",
        "schema_types": [
            "session.event.v2",
            "session.observe.close.v2",
            "session.observe.open.v2",
            "session.snapshot.v2",
            "stream.ack.v2",
            "stream.nack.v2",
        ],
    }


def _required_artifacts() -> tuple[Path, Path]:
    wheels = tuple(DIST_DIRECTORY.glob("hermes_connector-*.whl"))
    sdists = tuple(DIST_DIRECTORY.glob("hermes_connector-*.tar.gz"))
    assert len(wheels) == 1, "dist must contain exactly one Connector wheel"
    assert len(sdists) == 1, "dist must contain exactly one Connector sdist"
    return wheels[0], sdists[0]


def _artifact_members(artifact: Path) -> dict[str, bytes]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return {
                member: archive.read(member)
                for member in archive.namelist()
                if not member.endswith("/")
            }
    with tarfile.open(artifact, "r:gz") as archive:
        return {
            member.name: extracted.read()
            for member in archive.getmembers()
            if member.isfile()
            and (extracted := archive.extractfile(member)) is not None
        }


def _required_member(members: dict[str, bytes], source_name: str) -> bytes:
    package_name = source_name.removeprefix("src/")
    matches = [
        content
        for member, content in members.items()
        if member == package_name or member.endswith(f"/{source_name}")
    ]
    assert len(matches) == 1, f"artifact must contain exactly one {source_name}"
    return matches[0]


def _is_test_harness(member: str) -> bool:
    parts = PurePosixPath(member).parts
    return (
        "tests" in parts
        or PurePosixPath(member).name == "conftest.py"
        or any(part.startswith(".pytest") for part in parts)
    )


def _artifact_import_root(artifact: Path, tmp_path: Path) -> Path:
    if artifact.suffix == ".whl":
        return artifact
    with tarfile.open(artifact, "r:gz") as archive:
        archive.extractall(tmp_path)
    source_roots = tuple(tmp_path.glob("*/src"))
    assert len(source_roots) == 1
    return source_roots[0]
