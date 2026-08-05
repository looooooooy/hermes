from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures/repository_contracts"
PROVENANCE = FIXTURE_ROOT / "PROVENANCE.json"
HOST_SPI_ROOT = Path(__file__).parent / "fixtures/hermes_core_host_spi_v1"
HOST_SPI_PROVENANCE = HOST_SPI_ROOT / "PROVENANCE.json"
WORKSPACE_ROOT = Path(__file__).parents[2]


def test_repository_contract_fixtures_match_the_declared_provenance() -> None:
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert set(provenance) == {"schema_version", "source_scope", "files"}
    assert provenance["schema_version"] == 1
    assert provenance["source_scope"] == "repository-contract-fixtures"
    records = provenance["files"]
    assert isinstance(records, list)
    assert records
    assert [record["path"] for record in records] == sorted(
        record["path"] for record in records
    )
    expected = {record["path"] for record in records}
    actual = {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path != PROVENANCE
    }
    assert actual == expected
    for record in records:
        assert set(record) == {"path", "source", "sha256"}
        assert record["source"] == f"contracts/{record['path']}"
        payload = (FIXTURE_ROOT / record["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]


def test_host_spi_fixture_is_extracted_from_the_locked_stage1_patch() -> None:
    provenance = json.loads(HOST_SPI_PROVENANCE.read_text(encoding="utf-8"))
    assert set(provenance) == {
        "schema_version",
        "source_scope",
        "upstream",
        "stage1_patch",
        "files",
    }
    assert provenance["schema_version"] == 1
    assert provenance["source_scope"] == "hermes-core-host-spi-stage1-fixture"
    upstream = provenance["upstream"]
    upstream_lock_path = WORKSPACE_ROOT / upstream["lock_path"]
    upstream_lock_payload = upstream_lock_path.read_bytes()
    assert hashlib.sha256(upstream_lock_payload).hexdigest() == upstream["lock_sha256"]
    upstream_lock = json.loads(upstream_lock_payload)
    assert upstream_lock["upstream"] == {
        "distribution": "hermes-agent",
        "repository": upstream["repository"],
        "version": upstream["version"],
        "commit": upstream["commit"],
    }

    patch_record = provenance["stage1_patch"]
    patch_path = WORKSPACE_ROOT / patch_record["path"]
    patch_payload = patch_path.read_bytes()
    assert hashlib.sha256(patch_payload).hexdigest() == patch_record["sha256"]
    assert upstream_lock["patches"][0]["sha256"] == patch_record["sha256"]
    assert (
        Path(upstream["lock_path"]).parent
        / upstream_lock["patches"][0]["path"]
    ).as_posix() == patch_record["path"]

    integration_lock = json.loads(
        (WORKSPACE_ROOT / "hermes-cloud/deploy/test_server/integration-source-lock.json")
        .read_text(encoding="utf-8")
    )
    locked_inputs = {
        record["path"]: record["sha256"] for record in integration_lock["files"]
    }
    assert locked_inputs[upstream["lock_path"]] == upstream["lock_sha256"]
    assert locked_inputs[patch_record["path"]] == patch_record["sha256"]

    records = provenance["files"]
    assert records == [
        {
            "path": "hermes_cli/extension_host_v1.py",
            "extracted_from": "hermes_cli/extension_host_v1.py",
            "sha256": records[0]["sha256"],
        }
    ]
    extracted = _extract_new_file_from_patch(
        patch_payload.decode("utf-8"),
        records[0]["extracted_from"],
    )
    fixture = (HOST_SPI_ROOT / records[0]["path"]).read_bytes()
    assert extracted == fixture
    assert hashlib.sha256(extracted).hexdigest() == records[0]["sha256"]


def _extract_new_file_from_patch(patch: str, target: str) -> bytes:
    header = f"diff --git a/{target} b/{target}\n"
    start = patch.index(header)
    end = patch.find("\ndiff --git ", start + len(header))
    section = patch[start:] if end == -1 else patch[start : end + 1]
    assert "new file mode 100644\n" in section
    hunk = section.index("@@ -0,0 ")
    lines = section[hunk:].splitlines()[1:]
    payload_lines = []
    for line in lines:
        if line.startswith("+"):
            payload_lines.append(line[1:])
        elif line == "\\ No newline at end of file":
            continue
        else:
            assert line.startswith("diff --git "), line
            break
    return ("\n".join(payload_lines) + "\n").encode("utf-8")
