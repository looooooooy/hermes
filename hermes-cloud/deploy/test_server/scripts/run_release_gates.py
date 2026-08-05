#!/usr/bin/env python3
"""Run the complete release gate contract and write content-addressed evidence."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CLOUD_ROOT = Path(__file__).resolve().parents[3]
RELEASE_BUILDER = CLOUD_ROOT / "deploy/test_server/scripts/build_release.py"


class GateRunError(RuntimeError):
    """Raised when a release gate fails or cannot produce valid evidence."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run_gate(
    selection: dict[str, object],
    *,
    environment: dict[str, str],
    release_builder: dict[str, object],
    integration_snapshot: dict[str, object],
) -> tuple[dict[str, object], bytes, bytes, dict[str, object], bytes, bytes]:
    logical_argv = tuple(str(value) for value in selection["argv"])  # type: ignore[union-attr]
    argv = tuple(sys.executable if value == "{python}" else value for value in logical_argv)
    gate_environment = dict(environment)
    gate_environment.pop("PYTHONPATH", None)
    if selection["selection_id"] == "required_integration":
        snapshot_directory = integration_snapshot["directory"]
        snapshot_identity = integration_snapshot["identity"]
        assert isinstance(snapshot_directory, Path)
        assert isinstance(snapshot_identity, dict)
        gate_environment["HERMES_INTEGRATION_SNAPSHOT_ROOT"] = str(
            snapshot_directory
        )
        gate_environment["HERMES_INTEGRATION_SNAPSHOT_ID"] = str(
            snapshot_identity["snapshot_id"]
        )
    completed = subprocess.run(
        argv,
        cwd=CLOUD_ROOT,
        env=gate_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    normalize = release_builder["normalize_gate_output"]
    raw_stdout_payload = completed.stdout.encode("utf-8")
    raw_stderr_payload = completed.stderr.encode("utf-8")
    release_builder["validate_raw_gate_output"](raw_stdout_payload)  # type: ignore[operator]
    release_builder["validate_raw_gate_output"](raw_stderr_payload)  # type: ignore[operator]
    stdout = normalize(completed.stdout)  # type: ignore[operator]
    stderr = normalize(completed.stderr)  # type: ignore[operator]
    parse = release_builder["parse_gate_result"]
    parsed = parse(  # type: ignore[operator]
        selection,
        stdout=stdout,
        stderr=stderr,
        exit_code=completed.returncode,
    )
    stdout_payload = stdout.encode("utf-8")
    stderr_payload = stderr.encode("utf-8")
    canonical = release_builder["_canonical_json"]
    selection_id = str(selection["selection_id"])
    record = {
        "selection_id": selection_id,
        "kind": selection["kind"],
        "expected_count": selection["expected_count"],
        "argv": list(logical_argv),
        "argv_sha256": _sha256(canonical(list(logical_argv))),  # type: ignore[operator]
        **parsed,
        "stdout_file": f"{selection_id}.stdout",
        "stdout_normalized_sha256": _sha256(stdout_payload),
        "stderr_file": f"{selection_id}.stderr",
        "stderr_normalized_sha256": _sha256(stderr_payload),
    }
    raw_record = {
        "selection_id": selection_id,
        "stdout_file": f"{selection_id}.raw.stdout",
        "stdout_sha256": _sha256(raw_stdout_payload),
        "stdout_normalized_sha256": _sha256(stdout_payload),
        "stderr_file": f"{selection_id}.raw.stderr",
        "stderr_sha256": _sha256(raw_stderr_payload),
        "stderr_normalized_sha256": _sha256(stderr_payload),
    }
    return (
        record,
        stdout_payload,
        stderr_payload,
        raw_record,
        raw_stdout_payload,
        raw_stderr_payload,
    )


def _persist_raw_audit(
    stage: Path,
    *,
    audit_id: str,
    archive_payload: bytes,
    release_builder: dict[str, object],
) -> Path:
    root = release_builder["RAW_GATE_AUDIT_DIRECTORY"]
    assert isinstance(root, Path)
    root.mkdir(parents=True, exist_ok=True)
    archive = stage / f"{audit_id}.tar.gz"
    release_builder["_write_private_file"](archive, archive_payload)  # type: ignore[operator]
    try:
        destination = release_builder["atomic_promote_version"](  # type: ignore[operator]
            stage,
            root,
            audit_id,
        )
    except release_builder["ReleaseBuildError"] as error:
        raise GateRunError("raw gate audit replacement is unavailable") from error
    flat = root / archive.name
    if flat.exists() or flat.is_symlink():
        if flat.is_symlink() or not flat.is_file():
            raise GateRunError("raw gate audit replacement is unavailable")
        os.chmod(flat, 0o600)
    flat.write_bytes(archive_payload)
    os.chmod(flat, 0o600)
    return destination / archive.name


def _materialize_flat_evidence(
    evidence_directory: Path,
    store_root: Path,
    release_builder: dict[str, object],
) -> None:
    """Refresh the flat current-evidence files consumed by the bundle builder
    and release tests alongside the content-addressed versioned store."""
    for entry in sorted(evidence_directory.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            raise GateRunError("release gate evidence replacement is unavailable")
        payload = entry.read_bytes()
        flat = store_root / entry.name
        if flat.exists() or flat.is_symlink():
            if flat.is_symlink() or not flat.is_file():
                raise GateRunError(
                    "release gate evidence replacement is unavailable"
                )
            os.chmod(flat, 0o600)
            flat.write_bytes(payload)
        else:
            flat.write_bytes(payload)
        os.chmod(flat, 0o444)


def _run_gates_locked(environment: dict[str, str]) -> tuple[dict[str, object], Path]:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    toolchain = release_builder["require_build_environment"](environment)  # type: ignore[operator]
    toolchain_observation = release_builder["_toolchain_audit_observation"](
        environment
    )
    source_tree = release_builder["_source_tree_identity"]()  # type: ignore[operator]
    integration_snapshot = release_builder["create_integration_source_snapshot"]()
    integration_source = release_builder["validate_integration_source_inputs"]()
    selections = release_builder["GATE_SELECTIONS"]
    output = release_builder["GATE_EVIDENCE_DIRECTORY"]
    assert isinstance(output, Path)
    if output.exists() or output.is_symlink():
        try:
            release_builder["validate_gate_evidence_payloads"](
                release_builder["_local_gate_evidence_payloads"](),
                source_tree=source_tree,
                integration_source=integration_source,
                toolchain=toolchain,
            )
        except release_builder["ReleaseBuildError"]:
            try:
                current = release_builder["current_version_directory"](output)
                release_builder["validate_bootstrap_gate_evidence_directory"](
                    current
                )
            except release_builder["ReleaseBuildError"]:
                raise GateRunError(
                    "release gate evidence store is unavailable"
                ) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = release_builder["DIST_ROOT"]
    assert isinstance(stage_parent, Path)
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".release-evidence-", dir=stage_parent))
    raw_stage = Path(tempfile.mkdtemp(prefix=".release-raw-audit-", dir=stage_parent))
    try:
        records: list[dict[str, object]] = []
        raw_records: list[dict[str, object]] = []
        for selection in selections:  # type: ignore[union-attr]
            record, stdout, stderr, raw_record, raw_stdout, raw_stderr = _run_gate(
                selection,
                environment=environment,
                release_builder=release_builder,
                integration_snapshot=integration_snapshot,
            )
            records.append(record)
            raw_records.append(raw_record)
            (stage / str(record["stdout_file"])).write_bytes(stdout)
            (stage / str(record["stderr_file"])).write_bytes(stderr)
            (raw_stage / str(raw_record["stdout_file"])).write_bytes(raw_stdout)
            (raw_stage / str(raw_record["stderr_file"])).write_bytes(raw_stderr)
        evidence = {
            "schema_version": release_builder["GATE_EVIDENCE_SCHEMA_VERSION"],
            "deterministic": True,
            "attestation": "untrusted/self-recorded",
            "trust_scope": "integrity-and-replay-only",
            "normalized_output_scope": "exact-pytest-unittest-summary-lines-only",
            "generated_at_utc": release_builder["_release_timestamp"](),  # type: ignore[operator]
            "source_date_epoch": release_builder["SOURCE_DATE_EPOCH"],
            "source_tree": source_tree,
            "integration_source": integration_source,
            "toolchain": toolchain,
            "selection_contract_sha256": release_builder[
                "gate_selection_contract_sha256"
            ](),
            "selections": records,
        }
        evidence["evidence_set_sha256"] = release_builder[
            "gate_evidence_set_sha256"
        ](evidence)
        (stage / str(release_builder["GATE_EVIDENCE_NAME"])).write_text(
            json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        raw_audit = {
            "schema_version": release_builder["RAW_AUDIT_SCHEMA_VERSION"],
            "attestation": "untrusted/self-recorded",
            "trust_scope": "diagnostic-capture-only/non-stable/non-release-identity",
            "generated_at_utc": release_builder["_release_timestamp"](),  # type: ignore[operator]
            "source_date_epoch": release_builder["SOURCE_DATE_EPOCH"],
            "source_tree": source_tree,
            "integration_source": integration_source,
            "toolchain": toolchain,
            "toolchain_observation": toolchain_observation,
            "selection_contract_sha256": release_builder[
                "gate_selection_contract_sha256"
            ](),
            "selections": raw_records,
        }
        raw_audit["raw_audit_set_sha256"] = release_builder[
            "raw_gate_audit_set_sha256"
        ](raw_audit)
        (raw_stage / str(release_builder["RAW_GATE_AUDIT_NAME"])).write_text(
            json.dumps(raw_audit, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        payloads = {
            release_builder["_gate_payload_name"](path.name): path.read_bytes()
            for path in stage.iterdir()
        }
        release_builder["validate_gate_evidence_payloads"](
            payloads,
            source_tree=source_tree,
            integration_source=integration_source,
            toolchain=toolchain,
        )
        raw_payloads = {path.name: path.read_bytes() for path in raw_stage.iterdir()}
        release_builder["validate_raw_gate_audit_payloads"](
            raw_payloads,
            source_tree=source_tree,
            integration_source=integration_source,
            toolchain=toolchain,
        )
        raw_archive = release_builder["_raw_gate_audit_archive"](raw_payloads)
        audit_id = str(raw_audit["raw_audit_set_sha256"])
        raw_archive_path = _persist_raw_audit(
            raw_stage,
            audit_id=audit_id,
            archive_payload=raw_archive,
            release_builder=release_builder,
        )
        live_identity = release_builder["validate_integration_source_inputs"]()
        snapshot_identity = release_builder["verify_integration_snapshot_binding"](
            release_builder["validate_integration_snapshot_archive"](
                integration_snapshot["archive"]
            ),
            integration_snapshot["archive"],
        )
        if (
            snapshot_identity.get("file_count") != live_identity.get("file_count")
            or snapshot_identity.get("source_sha256") != live_identity.get("sha256")
        ):
            raise GateRunError("integration snapshot drifted during release gates")
        evidence_directory = release_builder["atomic_promote_version"](
            stage,
            output,
            str(evidence["evidence_set_sha256"]),
        )
        _materialize_flat_evidence(evidence_directory, output, release_builder)
        return evidence, raw_archive_path
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        if raw_stage.exists():
            shutil.rmtree(raw_stage)
        raise


def run_gates(environment: dict[str, str]) -> tuple[dict[str, object], Path]:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    lock_path = release_builder["DIST_ROOT"] / ".release-state.lock"
    try:
        with release_builder["exclusive_release_lock"](
            lock_path,
            timeout_seconds=5.0,
        ):
            return _run_gates_locked(environment)
    except release_builder["ReleaseBuildError"] as error:
        raise GateRunError("release gate lock unavailable") from error


def main() -> int:
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    try:
        evidence, raw_archive = run_gates(environment)
    except (GateRunError, OSError, subprocess.SubprocessError, ValueError, RuntimeError):
        print("release gates failed", file=sys.stderr)
        return 78
    print(
        "release_gates=PASS "
        f"evidence_sha256={evidence['evidence_set_sha256']} "
        f"raw_audit_archive={raw_archive} "
        f"raw_audit_archive_sha256={_sha256(raw_archive.read_bytes())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
