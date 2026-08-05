from __future__ import annotations

import hashlib
import json
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

CLOUD_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = (
    CLOUD_ROOT
    / "deploy"
    / "test_server"
    / "scripts"
    / "generate_integration_source_lock.py"
)
RELEASE_BUILDER = (
    CLOUD_ROOT / "deploy" / "test_server" / "scripts" / "build_release.py"
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(workspace: Path, relative: str, payload: bytes) -> None:
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    workspace = tmp_path / "workspace"
    patch_path = (
        "upstream/hermes-core-host-spi-v1/patches/"
        "0001-gateway-extension-host-spi-v1-stage1.patch"
    )
    patch_payload = (
        b"diff --git a/hermes_cli/extension_host_v1.py "
        b"b/hermes_cli/extension_host_v1.py\n"
        b"new file mode 100644\n"
        b"index 0000000..1111111\n"
        b"--- /dev/null\n"
        b"+++ b/hermes_cli/extension_host_v1.py\n"
        b"@@ -0,0 +1 @@\n"
        b"+HOST_SPI = 1\n"
    )
    fixture_payload = b"HOST_SPI = 1\n"
    upstream_lock_path = "upstream/hermes-core-host-spi-v1/upstream.lock.json"
    upstream = {
        "distribution": "hermes-agent",
        "repository": "https://github.com/NousResearch/hermes-agent.git",
        "version": "0.19.0",
        "commit": "14db1a99e21e5523ee61f10f5c3300a5087e8449",
    }
    upstream_lock = {
        "schema_version": 1,
        "stage": 3,
        "upstream": upstream,
        "patches": [
            {
                "path": "patches/0001-gateway-extension-host-spi-v1-stage1.patch",
                "sha256": _sha256(patch_payload),
            }
        ],
    }
    upstream_lock_payload = _json_bytes(upstream_lock)
    provenance = {
        "schema_version": 1,
        "source_scope": "hermes-core-host-spi-stage1-fixture",
        "upstream": {
            "repository": upstream["repository"],
            "version": upstream["version"],
            "commit": upstream["commit"],
            "lock_path": upstream_lock_path,
            "lock_sha256": "0" * 64,
        },
        "stage1_patch": {
            "path": patch_path,
            "sha256": _sha256(patch_payload),
        },
        "files": [
            {
                "path": "hermes_cli/extension_host_v1.py",
                "extracted_from": "hermes_cli/extension_host_v1.py",
                "sha256": _sha256(fixture_payload),
            }
        ],
    }

    _write(
        workspace,
        "hermes-agent-plugin/src/hermes_agent_plugin/plugin.py",
        b"plugin\n",
    )
    _write(
        workspace,
        "hermes-connector/src/hermes_connector/connector.py",
        b"connector\n",
    )
    for relative in (
        "tests/__init__.py",
        "tests/e2e/control_pipeline/__init__.py",
        "tests/e2e/control_pipeline/harness.py",
        "tests/e2e/plugin_test_runtime.py",
        "tests/test_support/__init__.py",
        "tests/test_support/host_spi_v1.py",
    ):
        _write(workspace, relative, f"{relative}\n".encode())
    _write(workspace, patch_path, patch_payload)
    _write(workspace, upstream_lock_path, upstream_lock_payload)
    _write(
        workspace,
        "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/"
        "hermes_cli/extension_host_v1.py",
        fixture_payload,
    )
    _write(
        workspace,
        "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/PROVENANCE.json",
        _json_bytes(provenance),
    )
    _write(
        workspace,
        "hermes-cloud/deploy/test_server/integration-source-lock.json",
        b'{"stale": true}\n',
    )
    return workspace, provenance


def _generator() -> dict[str, object]:
    return runpy.run_path(str(GENERATOR))


def test_generator_declaration_matches_the_release_builder() -> None:
    generator = _generator()
    release_builder = runpy.run_path(str(RELEASE_BUILDER))

    assert generator["INTEGRATION_SOURCE_ROOTS"] == release_builder[
        "_INTEGRATION_SOURCE_ROOTS"
    ]
    assert generator["INTEGRATION_SOURCE_EXACT_FILES"] == release_builder[
        "_INTEGRATION_SOURCE_EXACT_FILES"
    ]


def test_apply_hashes_exact_bytes_in_path_order_and_synchronizes_provenance(
    tmp_path: Path,
) -> None:
    workspace, original_provenance = _workspace(tmp_path)
    generator = _generator()

    changed = generator["synchronize_integration_source_lock"](workspace)

    assert changed is True
    lock_path = workspace / "hermes-cloud/deploy/test_server/integration-source-lock.json"
    lock = json.loads(lock_path.read_bytes())
    assert set(lock) == {"schema_version", "algorithm", "files"}
    assert lock["schema_version"] == 2
    assert lock["algorithm"] == "sha256-declared-integration-snapshot-v2"
    paths = [record["path"] for record in lock["files"]]
    assert paths == sorted(paths)
    assert paths == [
        "hermes-agent-plugin/src/hermes_agent_plugin/plugin.py",
        "hermes-connector/src/hermes_connector/connector.py",
        "tests/__init__.py",
        "tests/e2e/control_pipeline/__init__.py",
        "tests/e2e/control_pipeline/harness.py",
        "tests/e2e/plugin_test_runtime.py",
        "tests/test_support/__init__.py",
        "tests/test_support/host_spi_v1.py",
        (
            "upstream/hermes-core-host-spi-v1/patches/"
            "0001-gateway-extension-host-spi-v1-stage1.patch"
        ),
        "upstream/hermes-core-host-spi-v1/upstream.lock.json",
    ]
    for record in lock["files"]:
        assert record == {
            "path": record["path"],
            "sha256": _sha256((workspace / record["path"]).read_bytes()),
        }

    provenance_path = (
        workspace
        / "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/PROVENANCE.json"
    )
    provenance = json.loads(provenance_path.read_bytes())
    upstream_payload = (
        workspace / provenance["upstream"]["lock_path"]
    ).read_bytes()
    assert provenance["upstream"]["lock_sha256"] == _sha256(upstream_payload)
    expected = deepcopy(original_provenance)
    expected["upstream"]["lock_sha256"] = _sha256(upstream_payload)
    assert provenance == expected


def test_check_reports_drift_without_any_write(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    generator = _generator()
    lock_path = workspace / "hermes-cloud/deploy/test_server/integration-source-lock.json"
    provenance_path = (
        workspace
        / "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/PROVENANCE.json"
    )
    before = (lock_path.read_bytes(), provenance_path.read_bytes())

    def reject_replace(_source: Path, _target: Path) -> None:
        raise AssertionError("--check must not write")

    status = generator["main"](
        ["--check"], workspace_root=workspace, replace=reject_replace
    )

    assert status == 1
    assert (lock_path.read_bytes(), provenance_path.read_bytes()) == before


def test_fixture_must_exactly_match_the_current_stage1_patch_before_any_write(
    tmp_path: Path,
) -> None:
    workspace, _ = _workspace(tmp_path)
    generator = _generator()
    lock_path = workspace / "hermes-cloud/deploy/test_server/integration-source-lock.json"
    provenance_path = (
        workspace
        / "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/PROVENANCE.json"
    )
    fixture_path = (
        workspace
        / "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/"
        "hermes_cli/extension_host_v1.py"
    )
    fixture_path.write_bytes(b"not the patch payload\n")
    before = (lock_path.read_bytes(), provenance_path.read_bytes())

    with pytest.raises(generator["SourceLockError"], match="fixture"):
        generator["synchronize_integration_source_lock"](workspace)

    assert (lock_path.read_bytes(), provenance_path.read_bytes()) == before


def test_second_target_replace_failure_rolls_back_both_files(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    generator = _generator()
    lock_path = workspace / "hermes-cloud/deploy/test_server/integration-source-lock.json"
    provenance_path = (
        workspace
        / "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/PROVENANCE.json"
    )
    before = (lock_path.read_bytes(), provenance_path.read_bytes())
    real_replace = generator["os"].replace
    calls = 0

    def fail_second_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-target failure")
        real_replace(source, target)

    with pytest.raises(generator["SourceLockError"], match="transaction"):
        generator["synchronize_integration_source_lock"](
            workspace,
            replace=fail_second_replace,
        )

    assert calls >= 3
    assert (lock_path.read_bytes(), provenance_path.read_bytes()) == before
