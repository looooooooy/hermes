from __future__ import annotations

import json
import os
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from base64 import urlsafe_b64encode
from collections.abc import Callable
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

CLOUD_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_BUILDER = (
    CLOUD_ROOT / "deploy" / "test_server" / "scripts" / "build_sqlite_release_bundle.py"
)
RELEASE_BUILDER = (
    CLOUD_ROOT / "deploy" / "test_server" / "scripts" / "build_release.py"
)
GATE_RUNNER = (
    CLOUD_ROOT / "deploy" / "test_server" / "scripts" / "run_release_gates.py"
)
CANDIDATE_RUNNER = (
    CLOUD_ROOT
    / "deploy"
    / "test_server"
    / "sqlite"
    / "scripts"
    / "run_candidate_command.py"
)
SQLITE_README = CLOUD_ROOT / "deploy" / "test_server" / "sqlite" / "README.md"
TEST_SERVER_README = CLOUD_ROOT / "deploy" / "test_server" / "README.md"
ACTUAL_BUNDLE = CLOUD_ROOT / "dist" / "hermes-cloud-sqlite-release.tar.gz"
ACTUAL_WHEEL = CLOUD_ROOT / "dist" / "hermes_cloud-0.1.0-py3-none-any.whl"
ACTUAL_SDIST = CLOUD_ROOT / "dist" / "hermes_cloud-0.1.0.tar.gz"
ACTUAL_SDIST_HASH = CLOUD_ROOT / "dist" / "hermes_cloud-0.1.0.tar.gz.sha256"
ACTUAL_RELEASE_MANIFEST = CLOUD_ROOT / "dist" / "RELEASE-MANIFEST.json"
ACTUAL_SHA256SUMS = CLOUD_ROOT / "dist" / "SHA256SUMS"
RAW_AUDIT_ROOT = CLOUD_ROOT / "deploy" / "test_server" / "release_raw_audit"
METADATA_NAME = "hermes_cloud-0.1.0.dist-info/METADATA"
FROZEN_REV11_SHA256 = {
    "hermes_cloud-0.1.0-py3-none-any.whl": "23bb99a6286f7027f60634566703c7ee2b4c1107d6af83e6329412e16638704e",
    "hermes_cloud-0.1.0.tar.gz": "ff44bd2dccb00cf72874ad14126ce91fec2e90e4be6ceb77e380efc78a33c582",
    "hermes_cloud-0.1.0.tar.gz.sha256": "1a5758811e44b30a51b33eb2cbdd69d9ff32969503464deca39a77bf54050272",
    "hermes-cloud-sqlite-release.tar.gz": "be629678b6cc14260a95db45dd4af7829c2d5366de135f09870a80d607b62b8f",
    "RELEASE-MANIFEST.json": "ecd6cc1c09756bfe2b0845ba233a602b2a76c1cf7e03babf2eea2b9edabda03d",
    "SHA256SUMS": "52ca988db3e757f3d39ce77114cc0b0a92c6d76bd592b06e1710e0886c0d92a3",
}


def _actual_raw_audit_archive() -> Path:
    manifest = json.loads(ACTUAL_RELEASE_MANIFEST.read_text())
    archive = manifest["raw_gate_audit"]["archive"]
    filename = archive.get("filename")
    if not isinstance(filename, str):
        filename = Path(archive["path"]).name
    return RAW_AUDIT_ROOT / filename


def _mutated_actual_wheel(
    destination: Path,
    mutate: Callable[[dict[str, bytes]], None] = lambda _payloads: None,
) -> Path:
    with ZipFile(ACTUAL_WHEEL) as source:
        payloads = {name: source.read(name) for name in source.namelist()}
    record_name = "hermes_cloud-0.1.0.dist-info/RECORD"
    payloads.pop(record_name)
    mutate(payloads)
    record_lines = []
    for name, payload in sorted(payloads.items()):
        digest = urlsafe_b64encode(sha256(payload).digest()).rstrip(b"=").decode()
        record_lines.append(f"{name},sha256={digest},{len(payload)}")
    record_lines.append(f"{record_name},,")
    payloads[record_name] = ("\n".join(record_lines) + "\n").encode()
    with ZipFile(destination, "w") as output:
        for name, payload in sorted(payloads.items()):
            info = ZipInfo(name, date_time=(2026, 8, 2, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.create_version = 20
            info.extract_version = 20
            info.external_attr = (
                0o100644 if name.startswith("hermes_cloud/") else 0o644
            ) << 16
            output.writestr(info, payload)
    return destination


def _mutate_metadata(
    payloads: dict[str, bytes],
    mutate: Callable[[EmailMessage], None],
) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(payloads[METADATA_NAME])
    mutate(metadata)
    payloads[METADATA_NAME] = metadata.as_bytes(
        policy=policy.default.clone(linesep="\n", max_line_length=48)
    )


def _run_builder(wheel: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(BUNDLE_BUILDER),
            "--wheel",
            str(wheel),
            "--output",
            str(output),
        ),
        cwd=CLOUD_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_sqlite_readme_installs_a_no_dev_production_environment() -> None:
    readme = SQLITE_README.read_text()

    assert (
        'uv sync --project "$candidate_stage" --python /usr/bin/python3.11 '
        "\\\n  --frozen --no-dev --no-install-project" in readme
    )
    assert (
        'uv pip install --python "$candidate_stage/.venv/bin/python" --no-deps '
        "\\\n  \"$candidate_stage/artifacts/hermes_cloud-0.1.0-py3-none-any.whl\""
        in readme
    )
    assert "production `.venv` does not contain pytest or Ruff" in readme
    assert "58 standard-library `unittest` checks" in readme
    mint_selfcheck = (
        CLOUD_ROOT / "deploy" / "test_server" / "tests" / "test_mint_connector_token.py"
    ).read_text()
    assert "HmacJwtConnectorAuthenticator" in mint_selfcheck
    assert "SQLiteOperationScopedPairingRepository" in mint_selfcheck
    assert "set(claims)" in mint_selfcheck
    assert "test_owner_control_ttl_starts_after_a_slow_authority_query" in (
        mint_selfcheck
    )
    assert "test_owner_control_revalidates_after_external_revocation" in mint_selfcheck
    assert "test_owner_control_sqlite_orm_read_does_not_register_raw_pragma_hook" in (
        mint_selfcheck
    )
    assert "--inspect-binding" in mint_selfcheck


def test_sqlite_readme_blocks_stale_revision_11_compatibility_artifacts() -> None:
    readme = SQLITE_README.read_text()
    normalized = " ".join(readme.split())
    installation = readme.index("## Installation sequence")
    gate = readme.index("### Revision-11 compatibility artifact gate", installation)
    first_step = readme.index(
        "## Canonical revision-10 to revision-11 release runbook",
        installation,
    )

    assert installation < gate < first_step
    assert (
        "Existing `dist/` artifacts must not be used for this revision-11 "
        "compatibility fix." in normalized
    )
    assert (
        "rebuild the wheel, sdist, legacy sdist SHA-256, SQLite release bundle, "
        "release manifest, and standard checksum file from the current reviewed "
        "source" in normalized
    )
    for required_gate in (
        (
            "complete migration, compatibility, required cross-repository "
            "integration, architecture/distribution, stable Cloud, "
            "release-artifact, validation, and Ruff selections finish with zero "
            "failures"
        ),
        (
            "The current eight-selection baseline is 176 migration, 28 "
            "compatibility, 1 required integration, 62 release-artifact, 92 "
            "release-validation, 10 combined architecture/distribution, 1563 "
            "stable Cloud, and 1 Ruff result."
        ),
        (
            "If any selection grows, the full expanded selection must still finish "
            "with zero failures."
        ),
        "Ruff",
        "release validation",
        "Deployment is forbidden until every gate passes.",
    ):
        assert required_gate in normalized
    assert "at least 170 migration tests" not in normalized
    assert "at least 23 compatibility tests" not in normalized


def test_sqlite_readme_uses_the_current_cloud_local_release_evidence_counts() -> None:
    normalized = " ".join(SQLITE_README.read_text().split())

    assert "Cloud-local `1563 passed` and required integration `1 passed`" in normalized
    assert "Cloud `1452 passed`" not in normalized
    assert "root contracts `99 passed + 63 passed`" not in normalized


def test_sqlite_readme_has_one_command_level_rev10_to_rev11_runbook() -> None:
    readme = SQLITE_README.read_text()
    heading = "## Canonical revision-10 to revision-11 release runbook"
    assert readme.count(heading) == 1
    sequence = (
        "release-step-01-verify-manifest-and-candidate-venv",
        "release-step-02-stop-business",
        "release-step-03-stop-gateway",
        "release-step-04-confirm-stopped-before-backup",
        "release-step-05-backup-and-verify",
        "release-step-06-confirm-stopped-before-cleanup-plan",
        "release-step-07-cleanup-plan",
        "release-step-08-confirm-stopped-before-cleanup-apply",
        "release-step-09-cleanup-apply",
        "release-step-10-confirm-stopped-before-cleanup-absent",
        "release-step-11-cleanup-absent",
        "release-step-12-confirm-stopped-before-migration-plan",
        "release-step-13-migration-plan",
        "release-step-14-confirm-stopped-before-migration-apply",
        "release-step-15-migration-apply",
        "release-step-16-confirm-stopped-before-migration-current",
        "release-step-17-migration-current",
        "release-step-18-atomic-current-previous-switch",
        "release-step-19-daemon-reload",
        "release-step-20-start-gateway",
        "release-step-21-start-business",
        "release-step-22-direct-and-public-live-ready",
        "release-step-23-wss-and-owner-control-canary",
    )
    positions = [readme.index(marker) for marker in sequence]
    assert positions == sorted(positions)
    assert "switch the release, and reload systemd. Run and inspect the migration" not in readme
    assert "/opt/hermes-cloud/current/deploy/test_server/scripts/cleanup_test_seed_session.py" not in readme


def test_canonical_runbook_uses_an_exclusive_staging_candidate_and_safe_environment_runner() -> None:
    readme = SQLITE_README.read_text()
    runbook = readme.split(
        "## Canonical revision-10 to revision-11 release runbook", 1
    )[1].split("```bash", 1)[1].split("```", 1)[0]

    subprocess.run(
        ("bash", "-n"),
        input=runbook,
        text=True,
        check=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert 'test ! -e "$candidate_release"' in runbook
    assert 'candidate_stage="$releases_root/.candidate-$release_id-$$"' in runbook
    assert "umask 0027" in runbook
    assert 'mkdir -m 0750 -- "$candidate_stage"' in runbook
    assert 'chmod -R o-rwx,g+rX -- "$candidate_stage"' in runbook
    assert (
        'test "$(stat -c \'%U:%G:%a\' "$profile_environment")" = '
        'root:hermes-cloud:640'
    ) in runbook
    assert 'trap \'cleanup_candidate_stage\' EXIT' in runbook
    assert 'mv -T -- "$candidate_stage" "$candidate_release"' in runbook
    assert "--verify-only" in runbook
    assert (
        'raw_audit_archive="$raw_audit_root/$raw_audit_archive_sha256/'
        '$raw_audit_id.tar.gz"' in runbook
    )
    assert '--raw-audit-archive "$raw_audit_archive"' in runbook
    assert 'test "$(sha256sum "$raw_audit_archive" | cut -d\' \' -f1)" = ' in runbook
    normalized_readme = " ".join(readme.split())
    assert "raw audit archive is an out-of-band, non-core integrity record" in normalized_readme
    assert "must not be placed in `dist/` or the release directory" in normalized_readme
    assert "--candidate-release" in runbook
    assert "--environment-file" in runbook
    assert "HERMES_BOOTSTRAP_DSN_FILE" in runbook
    assert "HERMES_MIGRATION_DSN_FILE" in runbook
    assert "HERMES_RUNTIME_DSN_FILE" in runbook
    assert "HERMES_OBSERVER_KEYRING_FILE" in runbook
    assert "--require-seed-selectors" in runbook
    for executable in (
        ".venv/bin/python",
        "deploy/test_server/scripts/validate.sh",
        "deploy/test_server/sqlite/scripts/preflight.sh",
    ):
        assert f'--required-executable "$candidate_root/{executable}"' in runbook
    for readable in (
        "deploy/test_server/scripts/cleanup_test_seed_session.py",
        "deploy/test_server/scripts/migrate_sqlite.py",
        "deploy/test_server/sqlite/scripts/run_candidate_command.py",
    ):
        assert f'--required-readable "$candidate_root/{readable}"' in runbook
        assert f'--required-executable "$candidate_root/{readable}"' not in runbook
    assert "source " not in runbook
    assert ". /etc/" not in runbook
    promotion = runbook.index('mv -T -- "$candidate_stage" "$candidate_release"')
    cleanup = runbook.index("cleanup_test_seed_session.py", promotion)
    migration = runbook.index("migrate_sqlite.py", cleanup)
    assert promotion < cleanup < migration


def test_canonical_runbook_separates_root_orchestration_from_migration_operations() -> None:
    readme = SQLITE_README.read_text()
    runbook = readme.split(
        "## Canonical revision-10 to revision-11 release runbook", 1
    )[1].split("```bash", 1)[1].split("```", 1)[0]

    root_guard = runbook.index('test "$(id -u)" -eq 0')
    first_privileged_step = min(runbook.index("mkdir -m 0750"), runbook.index("systemctl stop"))
    assert root_guard < first_privileged_step
    assert "sudo" not in runbook
    normalized = " ".join(runbook.split())
    assert (
        "/usr/sbin/runuser --user hermes-cloud-migrate --group hermes-cloud "
        "--supp-group hermes-cloud -- /usr/bin/env -i" in normalized
    )
    assert (
        "/usr/sbin/runuser --user hermes-cloud --group hermes-cloud -- "
        "/usr/bin/env -i" in normalized
    )
    assert "--subject migration" in normalized
    assert "--subject runtime" in normalized
    assert (
        'test "$(stat -c \'%U:%G:%a\' "$HERMES_RUNTIME_DSN_FILE")" = '
        'hermes-cloud:hermes-cloud:600'
    ) in runbook
    for fixed_environment in (
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "HOME=/",
        "TMPDIR=/tmp",
        "PYTHONNOUSERSITE=1",
    ):
        assert fixed_environment in normalized
    assert "run_candidate_as_migrate" in runbook
    for data_command in (
        "validate.sh",
        "preflight.sh",
        "cleanup_test_seed_session.py",
        "migrate_sqlite.py",
    ):
        assert data_command in runbook
    non_root = subprocess.run(
        ("bash",),
        input=(
            "id() { if test \"${1:-}\" = -u; then echo 1000; "
            "else command id \"$@\"; fi; }\n" + runbook
        ),
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert non_root.returncode == 77
    assert "systemctl" not in non_root.stdout


def test_candidate_helper_requires_migration_identity_and_readable_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = runpy.run_path(str(CANDIDATE_RUNNER))

    monkeypatch.setattr(os, "geteuid", lambda: 991)
    monkeypatch.setattr(os, "getegid", lambda: 992)
    monkeypatch.setattr(os, "getgroups", lambda: [992])
    monkeypatch.setattr(
        runner["pwd"],
        "getpwuid",
        lambda _uid: type("User", (), {"pw_name": "hermes-cloud-migrate"})(),
    )
    monkeypatch.setattr(
        runner["grp"],
        "getgrgid",
        lambda _gid: type("Group", (), {"gr_name": "hermes-cloud"})(),
    )

    runner["require_execution_identity"]()
    readable = tmp_path / "migration-dsn"
    readable.write_text("not-a-real-secret")
    runner["require_readable_reference"](readable)
    link = tmp_path / "linked-dsn"
    link.symlink_to(readable)
    with pytest.raises(runner["CandidateCommandError"]):
        runner["require_readable_reference"](link)

    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o750)
    executable = candidate / "operation.py"
    executable.write_text("#!/usr/bin/env python3\n")
    executable.chmod(0o750)
    runner["require_candidate_executable"](candidate, executable)
    executable.chmod(0o640)
    with pytest.raises(runner["CandidateCommandError"]):
        runner["require_candidate_executable"](candidate, executable)
    with pytest.raises(runner["CandidateCommandError"]):
        runner["require_candidate_executable"](candidate, readable)

    monkeypatch.setattr(
        runner["pwd"],
        "getpwuid",
        lambda _uid: type("User", (), {"pw_name": "root"})(),
    )
    with pytest.raises(runner["CandidateCommandError"]):
        runner["require_execution_identity"]()


def test_candidate_helper_enforces_runtime_and_migration_subject_boundaries(
    tmp_path: Path,
) -> None:
    runner = runpy.run_path(str(CANDIDATE_RUNNER))
    bundle_builder = runpy.run_path(str(BUNDLE_BUILDER))
    modes = bundle_builder["REQUIRED_RELEASE_FILE_MODES"]
    assert modes["deploy/test_server/scripts/cleanup_test_seed_session.py"] == 0o644
    assert modes["deploy/test_server/scripts/migrate_sqlite.py"] == 0o644
    assert modes["deploy/test_server/sqlite/scripts/run_candidate_command.py"] == 0o644
    assert modes["deploy/test_server/scripts/validate.sh"] == 0o755
    assert modes["deploy/test_server/sqlite/scripts/preflight.sh"] == 0o755

    runtime_reference = tmp_path / "runtime_database_dsn"
    runtime_reference.write_text("not-a-real-secret")
    runtime_reference.chmod(0o600)
    runtime_stat = runtime_reference.stat()
    other_uid = runtime_stat.st_uid + 1
    assert runner["mode_allows"](
        runtime_stat,
        effective_uid=runtime_stat.st_uid,
        group_ids={runtime_stat.st_gid},
        permission=os.R_OK,
    )
    assert not runner["mode_allows"](
        runtime_stat,
        effective_uid=other_uid,
        group_ids={runtime_stat.st_gid},
        permission=os.R_OK,
    )
    assert stat.S_IMODE(runtime_stat.st_mode) == 0o600

    runtime_arguments = runner["_arguments"](
        [
            "--subject",
            "runtime",
            "--environment-file",
            str(tmp_path / "environment"),
            "--candidate-release",
            str(tmp_path / "candidate"),
            "--purpose",
            "validate",
            "--runtime-dsn-file",
            str(runtime_reference),
            "--",
            str(tmp_path / "candidate/.venv/bin/python"),
        ]
    )
    assert runtime_arguments.subject == "runtime"
    migration_arguments = runner["_arguments"](
        [
            "--subject",
            "migration",
            "--environment-file",
            str(tmp_path / "environment"),
            "--candidate-release",
            str(tmp_path / "candidate"),
            "--purpose",
            "validate",
            "--runtime-dsn-file",
            str(runtime_reference),
            "--",
            str(tmp_path / "candidate/.venv/bin/python"),
        ]
    )
    with pytest.raises(runner["CandidateCommandError"]):
        runner["validate_subject_contract"](migration_arguments)


def test_candidate_result_expectations_are_strict_state_machine_assertions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = runpy.run_path(str(CANDIDATE_RUNNER))
    session_id = "11111111-1111-4111-8111-111111111111"
    valid = {
        "cleanup-plan": (
            "cleanup_mode=plan status=ready schema_version=10 "
            f"session_id={session_id} sessions=1 messages=2 events=1 "
            "cursors=1 tickets=1\n"
        ),
        "cleanup-apply": (
            "cleanup_mode=apply status=removed schema_version=10 "
            f"session_id={session_id} sessions=1 messages=2 events=1 "
            "cursors=1 tickets=1\n"
        ),
        "cleanup-absent": (
            "cleanup_mode=plan status=absent schema_version=10 "
            f"session_id={session_id} sessions=0 messages=0 events=0 "
            "cursors=0 tickets=0\n"
        ),
        "migration-plan": (
            "sqlite_migration_mode=plan table_count=38 schema_version=11 "
            "historical_source_count=10 source=versioned-10 "
            "recent_two_covered=true\n"
        ),
        "migration-apply": (
            "sqlite_migration_mode=apply table_count=38 database_existing=true "
            "schema_version=11 source=versioned-10 recent_two_covered=true\n"
        ),
        "migration-current": (
            "sqlite_migration_mode=plan table_count=38 schema_version=11 "
            "historical_source_count=10 source=current recent_two_covered=true\n"
        ),
    }
    for expectation, payload in valid.items():
        assert runner["parse_expected_result"](expectation, payload)["status"] == "PASS"

    invalid = (
        ("cleanup-plan", valid["cleanup-absent"]),
        (
            "migration-plan",
            valid["migration-plan"].replace("source=versioned-10", "source=empty"),
        ),
        (
            "migration-plan",
            valid["migration-plan"].replace("source=versioned-10", "source=current"),
        ),
        ("cleanup-plan", valid["cleanup-plan"].replace("messages=2", "messages=1")),
        ("cleanup-plan", valid["cleanup-plan"].replace("status=ready", "status=ready status=ready")),
        ("migration-plan", valid["migration-plan"].replace(" source=", " extra=x source=")),
        ("migration-current", valid["migration-current"].replace(" schema_version=11", "")),
    )
    for expectation, payload in invalid:
        with pytest.raises(runner["CandidateCommandError"]):
            runner["parse_expected_result"](expectation, payload)

    cleanup_source = (
        CLOUD_ROOT / "deploy/test_server/scripts/cleanup_test_seed_session.py"
    ).read_text()
    assert '"schema_version=10 "' in cleanup_source

    monkeypatch.setitem(
        runner["main"].__globals__,
        "_command_environment",
        lambda _arguments: ({}, ("/bin/true",)),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=("/bin/true",),
            returncode=0,
            stdout=valid["cleanup-absent"],
            stderr="",
        ),
    )
    assert runner["main"](
        [
            "--environment-file",
            "/not-inspected",
            "--subject",
            "migration",
            "--candidate-release",
            "/not-inspected",
            "--purpose",
            "cleanup",
            "--expect",
            "cleanup-plan",
            "--",
            "/bin/true",
        ]
    ) == 78
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "candidate command rejected\n"


def test_candidate_environment_runner_binds_clean_commands_without_disclosing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = runpy.run_path(str(CANDIDATE_RUNNER))
    candidate = tmp_path / "releases" / "release-id"
    python = candidate / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    probe = candidate / "probe.py"
    probe.write_text("pass\n")
    environment_file = tmp_path / "test-server.env"
    environment_file.write_text(
        "HERMES_CURRENT=/opt/hermes-cloud/current\n"
        "HERMES_VENV=/opt/hermes-cloud/current/.venv\n"
        "HERMES_SEED_TENANT_SLUG=android-test\n"
        "HERMES_SEED_TENANT_DISPLAY_NAME=Android Test\n"
        "HERMES_SEED_USERNAME=android-user\n"
        "HERMES_SEED_USER_DISPLAY_NAME=Android User\n"
        "HERMES_SEED_WORKSPACE_KEY=android\n"
        "HERMES_SEED_WORKSPACE_DISPLAY_NAME=Android\n"
        "HERMES_SEED_OWNER_CONTROL_ENABLED=true\n"
        "HERMES_SEED_AGENT_KEY=android-agent\n"
        "HERMES_SEED_DEVICE_KEY=android-device\n"
    )
    monkeypatch.setitem(
        runner["_command_environment"].__globals__,
        "require_execution_identity",
        lambda _subject: "/",
    )
    inherited = {
        "NON_HERMES_SENTINEL": "random-parent-value",
        "AWS_SECRET_ACCESS_KEY": "not-a-real-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/parent/google.json",
        "AZURE_CLIENT_SECRET": "not-a-real-secret",
        "HTTP_PROXY": "http://user:password@parent.invalid",
        "HTTPS_PROXY": "http://user:password@parent.invalid",
        "API_TOKEN": "not-a-real-token",
        "PRIVATE_KEY": "not-a-real-key",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    arguments = runner["_arguments"](
        [
            "--environment-file",
            str(environment_file),
            "--subject",
            "migration",
            "--candidate-release",
            str(candidate),
            "--purpose",
            "validate",
            "--",
            str(python),
            str(probe),
        ]
    )

    environment, command = runner["_command_environment"](arguments)

    assert environment["HERMES_CURRENT"] == str(candidate)
    assert environment["HERMES_VENV"] == str(candidate / ".venv")
    assert environment["HERMES_SEED_TENANT_DISPLAY_NAME"] == "Android Test"
    assert "/opt/hermes-cloud/current" not in "\n".join(environment.values())
    assert set(environment) - {name for name in environment if name.startswith("HERMES_")} == {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "TMPDIR",
        "PYTHONNOUSERSITE",
    }
    assert environment["PATH"] == (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    assert environment["LANG"] == environment["LC_ALL"] == "C.UTF-8"
    assert environment["HOME"] == "/"
    assert environment["TMPDIR"] == "/tmp"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert set(inherited).isdisjoint(environment)
    assert command == (str(python), str(probe))

    rejected = runner["_arguments"](
        [
            "--environment-file",
            str(environment_file),
            "--subject",
            "migration",
            "--candidate-release",
            str(candidate),
            "--purpose",
            "validate",
            "--",
            "/opt/hermes-cloud/current/.venv/bin/python",
            str(probe),
        ]
    )
    with pytest.raises(runner["CandidateCommandError"]):
        runner["_command_environment"](rejected)


def test_generic_test_server_runbook_cannot_override_the_sqlite_upgrade_order() -> None:
    generic = TEST_SERVER_README.read_text()
    normalized = " ".join(generic.split())
    canonical_link = (
        "sqlite/README.md#canonical-revision-10-to-revision-11-release-runbook"
    )

    assert canonical_link in generic
    assert (
        "The generic operator sequence is forbidden for a SQLite revision-10 "
        "to revision-11 upgrade." in normalized
    )
    operator = generic.split("## Operator sequence", 1)[1].split("## ", 1)[0]
    assert "PostgreSQL profile only" in operator
    for document in (CLOUD_ROOT / "deploy" / "test_server").rglob("*.md"):
        if document == SQLITE_README:
            continue
        payload = " ".join(document.read_text().split())
        if "SQLite revision-10 to revision-11" in payload:
            assert canonical_link in document.read_text()


def test_sqlite_readme_requires_release_identity_and_automatic_checksum_pairing() -> None:
    readme = SQLITE_README.read_text()
    normalized = " ".join(readme.split())
    for required in (
        "SOURCE_DATE_EPOCH=1785628800",
        "RELEASE-MANIFEST.json",
        "SHA256SUMS",
        "sha256sum -c SHA256SUMS",
        "hermes_cloud-0.1.0.tar.gz.sha256",
        "release_id",
        "incoming/$release_id",
        "releases/$release_id",
        "bit-for-bit reproducibility is proven only for the fixed toolchain",
        "Package filenames and version `0.1.0` must never select a candidate release",
    ):
        assert required in normalized


def test_release_docs_require_independent_gate_replay_and_disclaim_attestation() -> None:
    readme = SQLITE_README.read_text()
    normalized = " ".join(readme.split())
    for required in (
        "attestation=untrusted/self-recorded",
        "integrity and replay record only",
        "does not prove that the recorded commands ran",
        "independent reviewer",
        "rerun all eight selections from the reviewed source tree",
        "must not accept bundled evidence as execution proof",
        "source identity, toolchain, selection IDs, and exact counts",
        "raw-output SHA-256",
        "excluded from `evidence_set_sha256`",
        "only exact pytest and unittest summary lines",
        "embeds one self-recorded replay",
        "does not prove two independent executions",
        "root:hermes-cloud` with mode `0640`",
        "fixed allowlist",
        "arbitrary parent environment variables are never copied",
        "The runtime database reference remains `hermes-cloud:hermes-cloud 0600`",
        "Raw audit files are a separate non-core collection",
        "raw audit archive and per-file SHA-256 digests",
        "fail closed before publication",
        "Absolute local paths are never normalized",
    ):
        assert required in normalized


def test_sqlite_readme_describes_the_bounded_collision_polling_window() -> None:
    readme = SQLITE_README.read_text()

    assert (
        "fixed-deadline bounded polling window of read-only revalidations" in readme
    )
    assert "performs one read-only revalidation" not in readme


def test_actual_sqlite_release_bundle_matches_current_reviewed_sources() -> None:
    builder = runpy.run_path(str(BUNDLE_BUILDER))
    manifest = json.loads(ACTUAL_RELEASE_MANIFEST.read_text())
    if manifest["schema_version"] == 1:
        assert {
            path.name: sha256(path.read_bytes()).hexdigest()
            for path in (
                ACTUAL_WHEEL,
                ACTUAL_SDIST,
                ACTUAL_SDIST_HASH,
                ACTUAL_BUNDLE,
                ACTUAL_RELEASE_MANIFEST,
                ACTUAL_SHA256SUMS,
            )
        } == FROZEN_REV11_SHA256
        return
    required_release_files = builder["REQUIRED_RELEASE_FILES"]
    expected_names = tuple(
        sorted(
            (
                f"hermes-cloud/artifacts/{ACTUAL_WHEEL.name}",
                *(f"hermes-cloud/{path}" for path in required_release_files),
            )
        )
    )

    assert ACTUAL_BUNDLE.is_file() and not ACTUAL_BUNDLE.is_symlink()
    assert ACTUAL_WHEEL.is_file() and not ACTUAL_WHEEL.is_symlink()
    with tarfile.open(ACTUAL_BUNDLE, "r:gz") as archive:
        members = tuple(member for member in archive.getmembers() if member.isfile())
        assert tuple(member.name for member in members) == expected_names
        payloads = {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in members
        }

    mismatched_sources = [
        relative
        for relative in required_release_files
        if payloads[f"hermes-cloud/{relative}"] != (CLOUD_ROOT / relative).read_bytes()
    ]
    assert mismatched_sources == []
    assert payloads[f"hermes-cloud/artifacts/{ACTUAL_WHEEL.name}"] == (
        ACTUAL_WHEEL.read_bytes()
    )

    builder["_validate_wheel"](ACTUAL_WHEEL)
    expected_production_payloads = builder["_expected_wheel_payloads"]()
    with ZipFile(ACTUAL_WHEEL) as wheel:
        production_payloads = {
            name: wheel.read(name)
            for name in wheel.namelist()
            if name.startswith("hermes_cloud/")
        }
    assert production_payloads == expected_production_payloads


def test_actual_sdist_is_current_hash_gated_audit_source(tmp_path: Path) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    manifest = json.loads(ACTUAL_RELEASE_MANIFEST.read_text())
    expected_digest = ACTUAL_SDIST_HASH.read_text().strip()

    assert len(expected_digest) == 64
    assert expected_digest == sha256(ACTUAL_SDIST.read_bytes()).hexdigest()
    if manifest["schema_version"] == 1:
        assert expected_digest == FROZEN_REV11_SHA256[ACTUAL_SDIST.name]
        with tarfile.open(ACTUAL_SDIST, "r:gz") as archive:
            names = archive.getnames()
        assert not any(Path(name).name == "t.md" for name in names)
        assert not any("release_raw_audit" in name for name in names)
        return
    with tarfile.open(ACTUAL_SDIST, "r:gz") as archive:
        members = {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
            if member.isfile()
        }
    root = "hermes_cloud-0.1.0/"
    required = (
        "deploy/test_server/scripts/cleanup_test_seed_session.py",
        "deploy/test_server/tests/test_cleanup_test_seed_session.py",
        "deploy/test_server/tests/test_cleanup_test_seed_session_release.py",
    )
    for relative in required:
        assert members[root + relative] == (CLOUD_ROOT / relative).read_bytes()
    release_sources = dict(release_builder["_release_source_files"]())
    assert set(members) == {
        root + relative.as_posix() for relative in release_sources
    } | {root + "PKG-INFO"}
    assert all(
        members[root + relative.as_posix()] == source.read_bytes()
        for relative, source in release_sources.items()
    )

    raw_markers = ("release_raw_audit", "hermes-cloud-release-raw-audit")
    with ZipFile(ACTUAL_WHEEL) as archive:
        assert not any(marker in name for name in archive.namelist() for marker in raw_markers)
    for artifact in (ACTUAL_SDIST, ACTUAL_BUNDLE):
        with tarfile.open(artifact, "r:gz") as archive:
            assert not any(
                marker in member.name
                for member in archive.getmembers()
                for marker in raw_markers
            )
    assert not any(
        marker in artifact.name
        for artifact in (
            ACTUAL_WHEEL,
            ACTUAL_SDIST,
            ACTUAL_SDIST_HASH,
            ACTUAL_BUNDLE,
            ACTUAL_RELEASE_MANIFEST,
            ACTUAL_SHA256SUMS,
        )
        for marker in raw_markers
    )

    project = tmp_path / "source-contract"
    shutil.copytree(
        CLOUD_ROOT,
        project,
        ignore=shutil.ignore_patterns(
            ".venv", ".pytest_cache", ".ruff_cache", "dist", "__pycache__", "*.pyc"
        ),
    )
    baseline = release_builder["_source_tree_identity"](project)
    (project / "t.md").write_text("unrelated root content changed\n")
    assert release_builder["_source_tree_identity"](project) == baseline
    raw_extra = project / "deploy/test_server/release_raw_audit/unrelated.raw"
    raw_extra.write_text("unrelated raw snapshot state\n")
    assert release_builder["_source_tree_identity"](project) == baseline
    runbook = project / "deploy/test_server/sqlite/README.md"
    runbook.write_text(runbook.read_text() + "\nrelease-input-mutation\n")
    assert release_builder["_source_tree_identity"](project) != baseline
    runbook.write_bytes((CLOUD_ROOT / "deploy/test_server/sqlite/README.md").read_bytes())
    added_test = project / "tests/release_source_contract_added.py"
    added_test.write_text("def test_release_source_contract_added(): pass\n")
    assert release_builder["_source_tree_identity"](project) != baseline

    readme = SQLITE_README.read_text()
    assert "auditable source distribution" in readme
    assert "not the runtime deployment artifact" in readme


def test_actual_release_manifest_and_standard_checksums_are_complete() -> None:
    manifest = json.loads(ACTUAL_RELEASE_MANIFEST.read_text())
    bundle_digest = sha256(ACTUAL_BUNDLE.read_bytes()).hexdigest()
    expected_artifacts = {
        path.name: {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in (ACTUAL_WHEEL, ACTUAL_SDIST, ACTUAL_SDIST_HASH, ACTUAL_BUNDLE)
    }

    assert manifest["schema_version"] in {1, 3}
    assert re.fullmatch(r"20260802T000000Z-[0-9a-f]{32}", manifest["release_id"])
    expected_algorithm = (
        "sha256-release-description-v2"
        if manifest["schema_version"] == 1
        else "sha256-release-description-v4"
    )
    assert manifest["release_identity"]["algorithm"] == expected_algorithm
    assert len(manifest["release_identity"]["description_sha256"]) == 64
    assert manifest["release_identity"]["suffix_length"] == 32
    assert manifest["release_id"].split("-", 1)[1] == (
        manifest["release_identity"]["description_sha256"][:32]
    )
    assert manifest["release_id"] != f"20260802T000000Z-{bundle_digest[:16]}"
    assert manifest["release_timestamp_utc"] == "2026-08-02T00:00:00Z"
    assert manifest["source_date_epoch"] == 1785628800
    assert manifest["package"] == {"name": "hermes-cloud", "version": "0.1.0"}
    assert {item["filename"]: {"bytes": item["bytes"], "sha256": item["sha256"]} for item in manifest["artifacts"]} == expected_artifacts
    assert ACTUAL_RELEASE_MANIFEST.name not in expected_artifacts
    assert manifest["manifest_integrity"] == {"provided_by": "SHA256SUMS", "self_digest": False}
    assert manifest["nested_artifacts"]["sqlite_release_bundle"]["wheel"] == {
        "path": f"hermes-cloud/artifacts/{ACTUAL_WHEEL.name}",
        "bytes": ACTUAL_WHEEL.stat().st_size,
        "sha256": expected_artifacts[ACTUAL_WHEEL.name]["sha256"],
    }
    assert manifest["source_tree"]["algorithm"] == (
        "sha256-release-source-allowlist-v1"
    )
    assert len(manifest["source_tree"]["sha256"]) == 64
    assert manifest["source_tree"]["file_count"] > 0
    assert manifest["source_tree"]["git"] == {
        "head": None,
        "tracked": False,
        "reason": "hermes-cloud is untracked in the enclosing checkout; Git is not release identity",
    }
    expected_toolchain = {
        "python": "CPython 3.12.11",
        "uv": "0.9.25",
        "hatchling": "1.31.0",
        "build": "1.5.0",
    }
    if manifest["schema_version"] == 1:
        expected_toolchain = {
            **expected_toolchain,
            "build_python": ".venv/bin/python",
            "uv": "uv 0.9.25 (38fcac0f3 2026-01-13)",
        }
    assert manifest["toolchain"] == expected_toolchain
    assert "gates" not in manifest
    gate_record = manifest["gate_evidence"]
    assert gate_record["attestation"] == "untrusted/self-recorded"
    assert gate_record["trust_scope"] == "integrity-and-replay-only"
    assert gate_record["path"] == (
        "hermes-cloud/deploy/test_server/release_evidence/GATE-EVIDENCE.json"
    )
    assert gate_record["selection_count"] == 8
    assert len(gate_record["sha256"]) == 64
    assert len(gate_record["evidence_set_sha256"]) == 64
    raw_audit = manifest["raw_gate_audit"]
    assert raw_audit["attestation"] == "untrusted/self-recorded"
    assert raw_audit["trust_scope"] == (
        "diagnostic-capture-only/non-stable/non-release-identity"
    )
    assert raw_audit["release_identity"] is False
    assert raw_audit["stable_evidence_set"] is False
    assert len(raw_audit["raw_audit_set_sha256"]) == 64
    assert len(raw_audit["archive"]["sha256"]) == 64
    assert len(raw_audit["files"]) == 17
    assert raw_audit["archive"]["filename"] == f"{raw_audit['audit_id']}.tar.gz"
    assert _actual_raw_audit_archive().is_file()
    assert raw_audit["archive"]["sha256"] == sha256(
        _actual_raw_audit_archive().read_bytes()
    ).hexdigest()
    assert all(
        item["path"].startswith("hermes-cloud-release-raw-audit/")
        for item in raw_audit["files"]
    )
    with tarfile.open(ACTUAL_BUNDLE, "r:gz") as archive:
        assert not any("release_raw_audit" in name for name in archive.getnames())
    assert not any(
        "raw-audit" in path.name.lower()
        for path in ACTUAL_SHA256SUMS.parent.iterdir()
        if path.is_file()
    )

    checksum_rows = [line.split("  ", 1) for line in ACTUAL_SHA256SUMS.read_text().splitlines()]
    assert [name for _digest, name in checksum_rows] == [
        ACTUAL_WHEEL.name,
        ACTUAL_SDIST.name,
        ACTUAL_SDIST_HASH.name,
        ACTUAL_BUNDLE.name,
        ACTUAL_RELEASE_MANIFEST.name,
    ]
    for digest, name in checksum_rows:
        assert digest == sha256((ACTUAL_SHA256SUMS.parent / name).read_bytes()).hexdigest()


def test_release_id_covers_the_complete_normalized_release_description() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    base = {
        "schema": "hermes-cloud-release-description-v2",
        "source_date_epoch": 1785628800,
        "package": {"name": "hermes-cloud", "version": "0.1.0"},
        "artifacts": [
            {"filename": f"artifact-{index}", "bytes": index, "sha256": char * 64}
            for index, char in enumerate("abcd", start=1)
        ],
        "source_tree": {
            "algorithm": "sha256-release-source-allowlist-v1",
            "sha256": "e" * 64,
            "file_count": 1,
        },
        "toolchain": {"python": "CPython 3.12.11", "hatchling": "1.31.0"},
        "gate_evidence": {"evidence_set_sha256": "f" * 64},
    }
    identities = {release_builder["release_id_from_description"](base)}
    mutations = []
    artifact = json.loads(json.dumps(base))
    artifact["artifacts"][0]["sha256"] = "0" * 64
    mutations.append(artifact)
    for section, key, value in (
        ("source_tree", "sha256", "1" * 64),
        ("toolchain", "hatchling", "1.31.1"),
        ("gate_evidence", "evidence_set_sha256", "2" * 64),
    ):
        mutation = json.loads(json.dumps(base))
        mutation[section][key] = value
        mutations.append(mutation)
    identities.update(
        release_builder["release_id_from_description"](mutation)
        for mutation in mutations
    )

    assert len(identities) == 5
    assert all(re.fullmatch(r"20260802T000000Z-[0-9a-f]{32}", value) for value in identities)
    with tarfile.open(ACTUAL_BUNDLE, "r:gz") as archive:
        assert "hermes-cloud/RELEASE-MANIFEST.json" not in archive.getnames()


def test_release_verifier_rejects_a_mutated_core_artifact(tmp_path: Path) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    release_directory = tmp_path / "release"
    release_directory.mkdir()
    for source in (
        ACTUAL_WHEEL,
        ACTUAL_SDIST,
        ACTUAL_SDIST_HASH,
        ACTUAL_BUNDLE,
        ACTUAL_RELEASE_MANIFEST,
        ACTUAL_SHA256SUMS,
    ):
        shutil.copy2(source, release_directory / source.name)
    raw_archive = tmp_path / _actual_raw_audit_archive().name
    shutil.copy2(_actual_raw_audit_archive(), raw_archive)
    release_builder["RAW_GATE_AUDIT_DIRECTORY"] = tmp_path / "absent-worktree-raw-state"

    with (release_directory / ACTUAL_BUNDLE.name).open("ab") as mutated:
        mutated.write(b"mutation")

    with pytest.raises(release_builder["ReleaseBuildError"], match="release artifact rejected"):
        release_builder["verify_release_directory"](
            release_directory,
            raw_audit_archive=raw_archive,
        )


def test_release_verifier_rejects_an_extra_candidate_file(tmp_path: Path) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    for source in (
        ACTUAL_WHEEL,
        ACTUAL_SDIST,
        ACTUAL_SDIST_HASH,
        ACTUAL_BUNDLE,
        ACTUAL_RELEASE_MANIFEST,
        ACTUAL_SHA256SUMS,
    ):
        shutil.copy2(source, tmp_path / source.name)
    (tmp_path / "unreviewed.txt").write_text("not reviewed\n")

    with pytest.raises(
        release_builder["ReleaseBuildError"], match="release directory rejected"
    ):
        release_builder["verify_release_directory"](tmp_path)


def test_uv_display_canonicalizes_mac_and_official_linux_forms() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    canonical_uv_version = release_builder.get("canonical_uv_version")

    assert canonical_uv_version is not None, "canonical uv parser is missing"
    assert canonical_uv_version("uv 0.9.25") == "0.9.25"
    assert (
        canonical_uv_version("uv 0.9.25 (38fcac0f3 2026-01-13)")
        == "0.9.25"
    )


def test_uv_display_rejects_wrong_versions_and_noncanonical_text() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    canonical_uv_version = release_builder.get("canonical_uv_version")

    assert canonical_uv_version is not None, "canonical uv parser is missing"
    for display in (
        "uv 0.9.24",
        "uv 0.9.25 ",
        " uv 0.9.25",
        "prefix uv 0.9.25",
        "uv 0.9.25 suffix",
        "uvx 0.9.25",
        "uv 0.9.25 (not-a-declared-build)",
        "uv",
        "",
    ):
        with pytest.raises(
            release_builder["ReleaseBuildError"],
            match="fixed release toolchain mismatch",
        ):
            canonical_uv_version(display)


def test_toolchain_identity_is_stable_and_rejects_undeclared_tools() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    canonical_toolchain_identity = release_builder.get(
        "canonical_toolchain_identity"
    )
    toolchain_audit_observation = release_builder.get(
        "toolchain_audit_observation"
    )
    assert canonical_toolchain_identity is not None, "toolchain canonicalizer is missing"
    assert toolchain_audit_observation is not None, "toolchain audit recorder is missing"

    common = {
        "python": "CPython 3.12.11",
        "build_python": ".venv/bin/python",
        "hatchling": "1.31.0",
        "build": "1.5.0",
    }
    mac = {**common, "uv_display": "uv 0.9.25 (38fcac0f3 2026-01-13)"}
    linux = {**common, "uv_display": "uv 0.9.25"}
    expected = {
        "python": "CPython 3.12.11",
        "uv": "0.9.25",
        "hatchling": "1.31.0",
        "build": "1.5.0",
    }

    assert canonical_toolchain_identity(mac) == expected
    assert canonical_toolchain_identity(linux) == expected
    assert toolchain_audit_observation(mac) == {
        "uv_display": "uv 0.9.25 (38fcac0f3 2026-01-13)"
    }
    assert toolchain_audit_observation(linux) == {"uv_display": "uv 0.9.25"}
    for undeclared in (
        {**mac, "cargo": "cargo 1.0.0"},
        {name: value for name, value in mac.items() if name != "build"},
        {**mac, "uv": "0.9.25"},
    ):
        with pytest.raises(
            release_builder["ReleaseBuildError"],
            match="fixed release toolchain mismatch",
        ):
            canonical_toolchain_identity(undeclared)


def test_new_release_schemas_exclude_display_metadata_and_reject_legacy_manifest() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    require_release_manifest_schema = release_builder.get(
        "require_release_manifest_schema"
    )

    assert release_builder.get("MANIFEST_SCHEMA_VERSION") == 3
    assert release_builder.get("GATE_EVIDENCE_SCHEMA_VERSION") == 5
    assert release_builder.get("RAW_AUDIT_SCHEMA_VERSION") == 3
    assert require_release_manifest_schema is not None, "manifest schema gate is missing"
    require_release_manifest_schema({"schema_version": 3})
    with pytest.raises(
        release_builder["ReleaseBuildError"],
        match="release manifest schema rejected",
    ):
        require_release_manifest_schema({"schema_version": 2})


def test_required_integration_source_identity_is_declared_and_fails_closed(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    declared_identity = release_builder.get(
        "declared_integration_source_identity"
    )
    validate_inputs = release_builder.get("validate_integration_source_inputs")
    assert declared_identity is not None, "integration identity reader is missing"
    assert validate_inputs is not None, "integration input validator is missing"

    workspace = tmp_path / "workspace"
    relative_files = (
        "hermes-agent-plugin/src/plugin.py",
        "hermes-connector/src/connector.py",
        "tests/__init__.py",
        "tests/e2e/control_pipeline/__init__.py",
        "tests/e2e/control_pipeline/harness.py",
        "tests/e2e/plugin_test_runtime.py",
        "tests/test_support/__init__.py",
        "tests/test_support/host_spi_v1.py",
        "upstream/hermes-core-host-spi-v1/upstream.lock.json",
        "upstream/hermes-core-host-spi-v1/patches/0001-gateway-extension-host-spi-v1-stage1.patch",
    )
    for index, relative in enumerate(relative_files):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixture-{index}\n", encoding="utf-8")

    def write_lock(path: Path) -> None:
        files = [
            {
                "path": relative,
                "sha256": sha256((workspace / relative).read_bytes()).hexdigest(),
            }
            for relative in relative_files
        ]
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "algorithm": "sha256-declared-integration-snapshot-v2",
                    "files": files,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    lock = tmp_path / "integration-source-lock.json"
    write_lock(lock)
    original_identity = declared_identity(lock)
    assert validate_inputs(lock, workspace) == original_identity

    plugin = workspace / relative_files[0]
    plugin.write_text("drift\n", encoding="utf-8")
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="integration source rejected"
    ):
        validate_inputs(lock, workspace)
    plugin.write_text("fixture-0\n", encoding="utf-8")
    plugin.unlink()
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="integration source rejected"
    ):
        validate_inputs(lock, workspace)
    plugin.write_text("fixture-0\n", encoding="utf-8")
    extra = workspace / "hermes-agent-plugin/src/undeclared.py"
    extra.write_text("undeclared\n", encoding="utf-8")
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="integration source rejected"
    ):
        validate_inputs(lock, workspace)
    extra.unlink()

    plugin.write_text("new-declared-input\n", encoding="utf-8")
    updated_lock = tmp_path / "updated-integration-source-lock.json"
    write_lock(updated_lock)
    assert declared_identity(updated_lock) != original_identity


def test_integration_snapshot_is_immutable_across_live_drift_and_restore(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    create_snapshot = release_builder.get("create_integration_source_snapshot")
    validate_snapshot = release_builder.get("validate_integration_snapshot_archive")
    assert create_snapshot is not None, "integration snapshot builder is missing"
    assert validate_snapshot is not None, "integration snapshot validator is missing"

    workspace, lock, relative_files = _integration_workspace(tmp_path)
    store = tmp_path / "snapshots"
    snapshot = create_snapshot(
        lock_path=lock,
        workspace_root=workspace,
        store_root=store,
    )
    plugin = workspace / relative_files[0]
    original = plugin.read_bytes()
    plugin.write_text("drift-during-gate\n", encoding="utf-8")
    assert (snapshot["directory"] / relative_files[0]).read_bytes() == original
    plugin.write_bytes(original)

    assert validate_snapshot(snapshot["archive"]) == snapshot["identity"]
    assert release_builder["validate_integration_source_inputs"](
        lock,
        workspace,
    ) == snapshot["declared_identity"]
    assert (store / "CURRENT").read_text(encoding="ascii") == (
        f"{snapshot['identity']['snapshot_id']}\n"
    )


def test_integration_snapshot_archive_is_content_addressed_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    create_snapshot = release_builder.get("create_integration_source_snapshot")
    validate_snapshot = release_builder.get("validate_integration_snapshot_archive")
    assert create_snapshot is not None
    assert validate_snapshot is not None

    workspace, lock, _relative_files = _integration_workspace(tmp_path)
    snapshot = create_snapshot(
        lock_path=lock,
        workspace_root=workspace,
        store_root=tmp_path / "snapshots",
    )
    assert snapshot["archive"].name == (
        f"{snapshot['identity']['snapshot_id']}.tar.gz"
    )

    tampered = tmp_path / snapshot["archive"].name
    tampered.write_bytes(snapshot["archive"].read_bytes() + b"tamper")
    with pytest.raises(
        release_builder["ReleaseBuildError"],
        match="integration snapshot rejected",
    ):
        validate_snapshot(tampered)


def test_final_snapshot_binding_verifies_without_live_siblings_and_checks_them_when_present(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    verify_binding = release_builder.get("verify_integration_snapshot_binding")
    assert verify_binding is not None, "final integration snapshot binding is missing"
    workspace, lock, relative_files = _integration_workspace(tmp_path)
    snapshot = release_builder["create_integration_source_snapshot"](
        lock_path=lock,
        workspace_root=workspace,
        store_root=tmp_path / "snapshots",
    )
    assert verify_binding(
        snapshot["identity"],
        snapshot["archive"],
        live_lock_path=None,
        live_workspace_root=None,
    ) == snapshot["identity"]

    plugin = workspace / relative_files[0]
    plugin.write_text("live-drift\n", encoding="utf-8")
    with pytest.raises(
        release_builder["ReleaseBuildError"],
        match="integration snapshot rejected",
    ):
        verify_binding(
            snapshot["identity"],
            snapshot["archive"],
            live_lock_path=lock,
            live_workspace_root=workspace,
        )


def test_verified_file_reader_rejects_parent_symlink_and_hardlink(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    read_verified = release_builder.get("read_verified_regular_file")
    assert read_verified is not None, "verified file reader is missing"

    real = tmp_path / "real"
    real.mkdir()
    source = real / "payload"
    source.write_bytes(b"reviewed")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(
        release_builder["ReleaseBuildError"],
        match="unsafe filesystem path",
    ):
        read_verified(linked_parent / "payload")

    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(
        release_builder["ReleaseBuildError"],
        match="unsafe filesystem path",
    ):
        read_verified(hardlink)


def test_candidate_snapshot_is_not_affected_by_post_validation_replacement(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    snapshot_directory = release_builder.get("snapshot_release_directory")
    assert snapshot_directory is not None, "candidate staging snapshot is missing"

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in release_builder["RELEASE_FILE_NAMES"]:
        (candidate / name).write_bytes(f"reviewed:{name}".encode())
    staged = snapshot_directory(candidate, tmp_path / "private-staging")
    target = candidate / release_builder["WHEEL_NAME"]
    target.write_bytes(b"replacement")

    assert (staged / release_builder["WHEEL_NAME"]).read_bytes() == (
        f"reviewed:{release_builder['WHEEL_NAME']}".encode()
    )


def test_release_lock_is_exclusive_and_bounded_across_processes(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    lock_script = release_builder.get("release_lock_probe_script")
    assert lock_script is not None, "cross-process release lock probe is missing"
    lock_path = tmp_path / "release.lock"
    owner = subprocess.Popen(
        [sys.executable, "-c", lock_script, str(lock_path), "1.0", "0.4"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "LOCKED"
        contender = subprocess.run(
            [sys.executable, "-c", lock_script, str(lock_path), "0.05", "0"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert contender.returncode == 78
        assert contender.stderr.strip() == "release lock unavailable"
    finally:
        owner.wait(timeout=2)
    assert owner.returncode == 0


def test_atomic_version_promotion_preserves_current_on_interruption(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    promote = release_builder.get("atomic_promote_version")
    assert promote is not None, "atomic version promotion is missing"
    store = tmp_path / "store"
    first = tmp_path / "stage-first"
    first.mkdir()
    (first / "payload").write_text("first", encoding="utf-8")
    promote(first, store, "a" * 64)
    assert (store / "CURRENT").read_text(encoding="ascii") == f"{'a' * 64}\n"

    second = tmp_path / "stage-second"
    second.mkdir()
    (second / "payload").write_text("second", encoding="utf-8")
    with pytest.raises(RuntimeError, match="injected before pointer"):
        promote(
            second,
            store,
            "b" * 64,
            before_pointer=lambda: (_ for _ in ()).throw(
                RuntimeError("injected before pointer")
            ),
        )
    assert (store / "CURRENT").read_text(encoding="ascii") == f"{'a' * 64}\n"
    assert (store / ("a" * 64) / "payload").read_text() == "first"


def _integration_workspace(
    tmp_path: Path,
) -> tuple[Path, Path, tuple[str, ...]]:
    workspace = tmp_path / "workspace"
    relative_files = (
        "hermes-agent-plugin/src/plugin.py",
        "hermes-connector/src/connector.py",
        "tests/__init__.py",
        "tests/e2e/control_pipeline/__init__.py",
        "tests/e2e/control_pipeline/harness.py",
        "tests/e2e/plugin_test_runtime.py",
        "tests/test_support/__init__.py",
        "tests/test_support/host_spi_v1.py",
        "upstream/hermes-core-host-spi-v1/upstream.lock.json",
        "upstream/hermes-core-host-spi-v1/patches/0001-gateway-extension-host-spi-v1-stage1.patch",
    )
    for index, relative in enumerate(relative_files):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixture-{index}\n", encoding="utf-8")
    records = [
        {
            "path": relative,
            "sha256": sha256((workspace / relative).read_bytes()).hexdigest(),
        }
        for relative in relative_files
    ]
    lock = tmp_path / "integration-source-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "algorithm": "sha256-declared-integration-snapshot-v2",
                "files": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return workspace, lock, relative_files


def test_gate_contract_separates_stable_cloud_from_required_integration() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    bundle_builder = runpy.run_path(str(BUNDLE_BUILDER))
    selections = {
        selection["selection_id"]: selection
        for selection in release_builder["GATE_SELECTIONS"]
    }
    assert tuple(selections) == (
        "migration",
        "compatibility",
        "required_integration",
        "release_artifacts",
        "release_validation",
        "architecture_distribution",
        "cloud",
        "ruff",
    )
    assert selections["required_integration"]["expected_count"] == 1
    assert selections["required_integration"]["argv"][-1] == (
        "tests/integration/test_web_connector_control_bridge_e2e.py::"
        "test_cookie_to_cloud_bridge_real_connector_lane_owner_actions"
    )
    assert selections["architecture_distribution"]["expected_count"] == 10
    assert selections["cloud"]["expected_count"] == 1563
    assert "--ignore=tests/integration/test_web_connector_control_bridge_e2e.py" in (
        selections["cloud"]["argv"]
    )
    assert bundle_builder["_GATE_SELECTION_IDS"] == tuple(selections)


def test_required_integration_gate_is_bound_to_the_immutable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    gate_runner = runpy.run_path(str(GATE_RUNNER))
    selection = next(
        item
        for item in release_builder["GATE_SELECTIONS"]
        if item["selection_id"] == "required_integration"
    )
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        observed["argv"] = tuple(argv)
        observed["environment"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "1 passed in 0.01s\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gate_runner["_run_gate"](
        selection,
        environment={"PATH": os.environ["PATH"], "PYTHONPATH": "/live/sibling"},
        release_builder=release_builder,
        integration_snapshot={
            "directory": snapshot_root,
            "identity": {"snapshot_id": "a" * 64},
        },
    )
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["HERMES_INTEGRATION_SNAPSHOT_ROOT"] == str(snapshot_root)
    assert environment["HERMES_INTEGRATION_SNAPSHOT_ID"] == "a" * 64
    assert "PYTHONPATH" not in environment


def test_release_gate_expected_counts_match_current_selections() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))

    release_artifacts = next(
        selection
        for selection in release_builder["GATE_SELECTIONS"]
        if selection["selection_id"] == "release_artifacts"
    )
    assert release_artifacts["argv"] == (
        "{python}",
        "-m",
        "pytest",
        "-q",
        "deploy/test_server/tests/test_sqlite_release_bundle.py",
    )

    assert {
        selection["selection_id"]: selection["expected_count"]
        for selection in release_builder["GATE_SELECTIONS"]
    } == {
        "migration": 176,
        "compatibility": 28,
        "required_integration": 1,
        "release_artifacts": 62,
        "release_validation": 92,
        "architecture_distribution": 10,
        "cloud": 1563,
        "ruff": 1,
    }


def test_release_builder_pins_the_complete_offline_toolchain_and_epoch() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    project = tomllib.loads((CLOUD_ROOT / "pyproject.toml").read_text())

    assert project["build-system"]["requires"] == ["hatchling==1.31.0"]
    assert "hatchling==1.31.0" in project["dependency-groups"]["dev"]
    assert "build==1.5.0" in project["dependency-groups"]["dev"]
    assert project["tool"]["hatch"]["build"]["targets"]["sdist"] == {
        "only-include": [
            ".gitignore",
            "README.md",
            "pyproject.toml",
            "uv.lock",
            "src",
            "tests",
            "deploy/test_server",
        ],
        "exclude": [
            "/deploy/test_server/release_evidence/**",
            "/deploy/test_server/release_raw_audit/**",
            "/deploy/test_server/integration_snapshots/**",
            "**/__pycache__/**",
            "**/*.pyc",
        ],
        "ignore-vcs": True,
    }
    assert release_builder["SOURCE_DATE_EPOCH"] == 1785628800
    assert release_builder["REQUIRED_PYTHON"] == "3.12.11"
    assert release_builder["REQUIRED_UV_VERSION"] == "0.9.25"
    assert release_builder["REQUIRED_HATCHLING"] == "1.31.0"
    assert release_builder["REQUIRED_BUILD"] == "1.5.0"
    with pytest.raises(release_builder["ReleaseBuildError"], match="SOURCE_DATE_EPOCH"):
        release_builder["require_build_environment"]({})
    build_command = release_builder["distribution_build_command"](
        Path("/verified/uv"), Path("/verified/python"), Path("/output")
    )
    assert build_command == (
        "/verified/uv",
        "build",
        "--offline",
        "--no-build-isolation",
        "--python",
        "/verified/python",
        "--out-dir",
        "/output",
    )
    assert "GATE_RESULTS" not in RELEASE_BUILDER.read_text()
    raw_gate_output = (
        "business_retry in 30s\n"
        "order_window=(0:01:59)\n"
        "FAILED diagnostic retry in 128.37s (0:01:59)\n"
        "1563 passed in 128.37s (0:01:59)\n"
        "Ran 90 tests in 1.234s\n"
        f"FAILED tenant_path={CLOUD_ROOT}/tenant-a\n"
    )
    assert release_builder["normalize_gate_output"](raw_gate_output) == (
        "business_retry in 30s\n"
        "order_window=(0:01:59)\n"
        "FAILED diagnostic retry in 128.37s (0:01:59)\n"
        "1563 passed in <DURATION>\n"
        "Ran 90 tests in <DURATION>\n"
        f"FAILED tenant_path={CLOUD_ROOT}/tenant-a\n"
    )

    with ZipFile(ACTUAL_WHEEL) as wheel:
        wheel_metadata = wheel.read("hermes_cloud-0.1.0.dist-info/WHEEL").decode()
    assert "Generator: hatchling 1.31.0\n" in wheel_metadata


def test_raw_gate_audit_is_saved_scanned_and_tamper_evident(tmp_path: Path) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    release_builder["validate_raw_gate_output"](b"43 passed in 10.84s\n")
    for sensitive in (
        b"token=not-a-real-token\n",
        b"https://user:password@example.invalid/path\n",
        f"FAILED path={CLOUD_ROOT}/tenant-a\n".encode(),
    ):
        with pytest.raises(
            release_builder["ReleaseBuildError"], match="raw gate audit rejected"
        ):
            release_builder["validate_raw_gate_output"](sensitive)

    payloads: dict[str, bytes] = {}
    records: list[dict[str, object]] = []
    for selection in release_builder["GATE_SELECTIONS"]:
        selection_id = selection["selection_id"]
        stdout = b"safe gate output\n"
        stderr = b""
        stdout_name = f"{selection_id}.raw.stdout"
        stderr_name = f"{selection_id}.raw.stderr"
        payloads[stdout_name] = stdout
        payloads[stderr_name] = stderr
        records.append(
            {
                "selection_id": selection_id,
                "stdout_file": stdout_name,
                "stdout_sha256": sha256(stdout).hexdigest(),
                "stdout_normalized_sha256": sha256(
                    release_builder["normalize_gate_output"](
                        stdout.decode("utf-8")
                    ).encode()
                ).hexdigest(),
                "stderr_file": stderr_name,
                "stderr_sha256": sha256(stderr).hexdigest(),
                "stderr_normalized_sha256": sha256(
                    release_builder["normalize_gate_output"](
                        stderr.decode("utf-8")
                    ).encode()
                ).hexdigest(),
            }
        )
    audit = {
        "schema_version": release_builder["RAW_AUDIT_SCHEMA_VERSION"],
        "attestation": "untrusted/self-recorded",
        "trust_scope": "diagnostic-capture-only/non-stable/non-release-identity",
        "generated_at_utc": release_builder["_release_timestamp"](),
        "source_date_epoch": release_builder["SOURCE_DATE_EPOCH"],
        "source_tree": release_builder["_source_tree_identity"](),
        "integration_source": release_builder[
            "declared_integration_source_identity"
        ](),
        "toolchain": release_builder["_toolchain"](os.environ),
        "toolchain_observation": release_builder[
            "_toolchain_audit_observation"
        ](os.environ),
        "selection_contract_sha256": release_builder[
            "gate_selection_contract_sha256"
        ](),
        "selections": records,
    }
    audit["raw_audit_set_sha256"] = release_builder[
        "raw_gate_audit_set_sha256"
    ](audit)
    payloads["RAW-AUDIT.json"] = (
        json.dumps(audit, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode()
    assert release_builder["validate_raw_gate_audit_payloads"](
        payloads,
        source_tree=audit["source_tree"],
        integration_source=audit["integration_source"],
        toolchain=audit["toolchain"],
    )["raw_audit_set_sha256"] == audit["raw_audit_set_sha256"]

    archive = tmp_path / f"{audit['raw_audit_set_sha256']}.tar.gz"
    archive.write_bytes(release_builder["_raw_gate_audit_archive"](payloads))
    archive.chmod(0o600)
    record = release_builder["_raw_gate_audit_record"](
        raw_audit_archive=archive,
        source_tree=release_builder["_source_tree_identity"](),
        integration_source=release_builder[
            "declared_integration_source_identity"
        ](),
        toolchain=release_builder["_toolchain"](os.environ),
    )
    assert record["archive"] == {
        "filename": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": sha256(archive.read_bytes()).hexdigest(),
    }
    tampered_archive = tmp_path / archive.name
    tampered_archive.write_bytes(archive.read_bytes() + b"mutation")
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="raw gate audit rejected"
    ):
        release_builder["_raw_gate_audit_record"](
            raw_audit_archive=tampered_archive,
            source_tree=release_builder["_source_tree_identity"](),
            integration_source=release_builder[
                "declared_integration_source_identity"
            ](),
            toolchain=release_builder["_toolchain"](os.environ),
        )

    forged = dict(payloads)
    forged[str(records[0]["stdout_file"])] += b"mutation\n"
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="raw gate audit rejected"
    ):
        release_builder["validate_raw_gate_audit_payloads"](
            forged,
            source_tree=audit["source_tree"],
            integration_source=audit["integration_source"],
            toolchain=audit["toolchain"],
        )


def test_release_gate_evidence_is_complete_content_addressed_and_not_static() -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    assert GATE_RUNNER.is_file()
    manifest = json.loads(ACTUAL_RELEASE_MANIFEST.read_text())
    with tarfile.open(ACTUAL_BUNDLE, "r:gz") as archive:
        evidence_payloads = {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
            if member.isfile()
            and member.name.startswith(
                "hermes-cloud/deploy/test_server/release_evidence/"
                )
        }
    evidence_name = (
        "hermes-cloud/deploy/test_server/release_evidence/GATE-EVIDENCE.json"
    )
    embedded_evidence = json.loads(evidence_payloads[evidence_name])
    if manifest["schema_version"] == 1:
        assert embedded_evidence["schema_version"] == 3
        assert embedded_evidence["source_tree"] == manifest["source_tree"]
        assert embedded_evidence["toolchain"] == manifest["toolchain"]
        assert embedded_evidence["evidence_set_sha256"] == release_builder[
            "gate_evidence_set_sha256"
        ](embedded_evidence)
        return

    evidence = release_builder["validate_gate_evidence_payloads"](
        evidence_payloads,
        source_tree=release_builder["_source_tree_identity"](),
        integration_source=release_builder[
            "declared_integration_source_identity"
        ](),
        toolchain=release_builder["_toolchain"](os.environ),
    )
    assert evidence["schema_version"] == 5
    assert evidence["deterministic"] is True
    assert evidence["attestation"] == "untrusted/self-recorded"
    assert evidence["trust_scope"] == "integrity-and-replay-only"
    assert evidence["normalized_output_scope"] == (
        "exact-pytest-unittest-summary-lines-only"
    )
    assert evidence["generated_at_utc"] == "2026-08-02T00:00:00Z"
    assert evidence["source_date_epoch"] == 1785628800
    assert [item["selection_id"] for item in evidence["selections"]] == [
        item["selection_id"] for item in release_builder["GATE_SELECTIONS"]
    ]
    assert all(item["exit_code"] == 0 for item in evidence["selections"])
    assert all(item["failed"] == 0 for item in evidence["selections"])
    assert all(item["passed"] == item["selected"] > 0 for item in evidence["selections"])
    assert {
        item["selection_id"]: item["selected"] for item in evidence["selections"]
    } == {
        "migration": 176,
        "compatibility": 28,
        "required_integration": 1,
        "release_artifacts": 62,
        "release_validation": 92,
        "architecture_distribution": 10,
        "cloud": 1563,
        "ruff": 1,
    }
    for item in evidence["selections"]:
        assert len(item["stdout_normalized_sha256"]) == 64
        assert len(item["stderr_normalized_sha256"]) == 64
        assert not any(name.endswith("_raw_sha256") for name in item)

    forged = dict(evidence_payloads)
    stdout_name = next(name for name in forged if name.endswith(".stdout"))
    forged[stdout_name] += b"forged\n"
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="gate evidence rejected"
    ):
        release_builder["validate_gate_evidence_payloads"](
            forged,
            source_tree=release_builder["_source_tree_identity"](),
            integration_source=release_builder[
                "declared_integration_source_identity"
            ](),
            toolchain=release_builder["_toolchain"](os.environ),
        )


def test_gate_runner_allows_only_bounded_schema4_bootstrap_evidence(
    tmp_path: Path,
) -> None:
    release_builder = runpy.run_path(str(RELEASE_BUILDER))
    validate_bootstrap = release_builder.get(
        "validate_bootstrap_gate_evidence_directory"
    )
    assert validate_bootstrap is not None, "bootstrap evidence gate is missing"

    with tarfile.open(ACTUAL_BUNDLE, "r:gz") as archive:
        payloads = {
            Path(member.name).name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
            if member.isfile()
            and member.name.startswith(
                "hermes-cloud/deploy/test_server/release_evidence/"
            )
        }
    evidence = json.loads(payloads["GATE-EVIDENCE.json"])
    evidence["schema_version"] = 4
    payloads["GATE-EVIDENCE.json"] = (
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode()
    for name, payload in payloads.items():
        (tmp_path / name).write_bytes(payload)

    validate_bootstrap(tmp_path)
    evidence["schema_version"] = 5
    (tmp_path / "GATE-EVIDENCE.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )
    validate_bootstrap(tmp_path)
    evidence["schema_version"] = 3
    (tmp_path / "GATE-EVIDENCE.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="gate evidence rejected"
    ):
        validate_bootstrap(tmp_path)
    evidence["schema_version"] = 4
    (tmp_path / "GATE-EVIDENCE.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )
    (tmp_path / "extra.stdout").write_text("not allowlisted\n")
    with pytest.raises(
        release_builder["ReleaseBuildError"], match="gate evidence rejected"
    ):
        validate_bootstrap(tmp_path)


def test_two_independent_fixed_toolchain_environments_reproduce_release(
    tmp_path: Path,
) -> None:
    manifest = json.loads(ACTUAL_RELEASE_MANIFEST.read_text())
    if manifest["schema_version"] == 1:
        release_builder = runpy.run_path(str(RELEASE_BUILDER))
        assert release_builder["canonical_uv_version"]("uv 0.9.25") == (
            release_builder["canonical_uv_version"](
                "uv 0.9.25 (38fcac0f3 2026-01-13)"
            )
        )
        return
    uv = shutil.which("uv")
    assert uv is not None
    release_names = (
        ACTUAL_WHEEL.name,
        ACTUAL_SDIST.name,
        ACTUAL_SDIST_HASH.name,
        ACTUAL_BUNDLE.name,
        ACTUAL_RELEASE_MANIFEST.name,
        ACTUAL_SHA256SUMS.name,
    )
    results: list[dict[str, str]] = []
    environments: list[Path] = []
    for index in (1, 2):
        project = tmp_path / f"clean-project-{index}"
        shutil.copytree(
            CLOUD_ROOT,
            project,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                "*.pyc",
                "dist",
            ),
        )
        subprocess.run(
            (
                uv,
                "sync",
                "--offline",
                "--frozen",
                "--no-install-project",
                "--python",
                sys.executable,
            ),
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        )
        environment = {**os.environ, "SOURCE_DATE_EPOCH": "1785628800"}
        raw_archive = (
            project
            / "deploy/test_server/release_raw_audit"
            / _actual_raw_audit_archive().name
        )
        if index == 2:
            (project / "deploy/test_server/release_raw_audit/unrelated.raw").write_text(
                "must not affect release identity or any core artifact\n"
            )
        subprocess.run(
            (
                str(project / ".venv/bin/python"),
                str(project / "deploy/test_server/scripts/build_release.py"),
                "--raw-audit-archive",
                str(raw_archive),
            ),
            cwd=project,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        environments.append((project / ".venv").resolve())
        results.append(
            {
                name: sha256((project / "dist" / name).read_bytes()).hexdigest()
                for name in release_names
            }
        )

    assert environments[0] != environments[1]
    assert results[0] == results[1]


def test_sqlite_release_builder_emits_only_the_reviewed_candidate_files(
    tmp_path: Path,
) -> None:
    wheel = _mutated_actual_wheel(tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl")
    output = tmp_path / "hermes-cloud-sqlite-release.tar.gz"

    completed = _run_builder(wheel, output)

    assert completed.returncode == 0, completed.stderr
    with tarfile.open(output, "r:gz") as archive:
        names = tuple(member.name for member in archive.getmembers())
    assert names == tuple(sorted(names)), "candidate files need a stable audit order"
    assert f"hermes-cloud/artifacts/{wheel.name}" in names
    assert "hermes-cloud/pyproject.toml" in names
    assert "hermes-cloud/uv.lock" in names
    assert (
        "hermes-cloud/deploy/test_server/scripts/cleanup_test_seed_session.py" in names
    )
    assert "hermes-cloud/deploy/test_server/scripts/migrate_sqlite.py" in names
    assert (
        "hermes-cloud/deploy/test_server/sqlite/nginx/hermes-test-server.conf" in names
    )
    assert "hermes-cloud/deploy/test_server/sqlite/env/test-server.env.example" in names
    assert (
        "hermes-cloud/deploy/test_server/tests/test_sqlite_deploy_artifacts.py" in names
    )
    validation_tests = {
        Path(name).name for name in names if "/deploy/test_server/tests/" in f"/{name}"
    }
    assert validation_tests == {
        "test_cleanup_test_seed_session_release.py",
        "test_mint_connector_token.py",
        "test_seed_test_data.py",
        "test_sqlite_deploy_artifacts.py",
    }
    assert "hermes-cloud/deploy/test_server/scripts/health.sh" not in names
    assert "hermes-cloud/deploy/test_server/tests/test_deploy_artifacts.py" not in names
    forbidden = (
        "__pycache__",
        ".pyc",
        "/._",
        ".DS_Store",
        "/secrets/",
        ".db",
        ".sqlite",
        ".sqlite3",
        "/connector.token",
    )
    for marker in forbidden:
        assert not any(marker in name for name in names), marker
    assert not any(Path(name).name == "test-server.env" for name in names)


def test_sqlite_release_builder_uses_declarative_member_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = runpy.run_path(str(BUNDLE_BUILDER))
    modes = builder["REQUIRED_RELEASE_FILE_MODES"]
    assert set(modes) == set(builder["REQUIRED_RELEASE_FILES"])
    assert set(modes.values()) <= {0o644, 0o755}
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("os.access is forbidden")))
    output = tmp_path / "candidate.tar.gz"

    builder["build_bundle"](ACTUAL_WHEEL, output)

    with tarfile.open(output, "r:gz") as archive:
        member_modes = {member.name: member.mode for member in archive.getmembers()}
    assert member_modes[f"hermes-cloud/artifacts/{ACTUAL_WHEEL.name}"] == 0o644
    for relative, mode in modes.items():
        assert member_modes[f"hermes-cloud/{relative}"] == mode


def test_actual_wheel_has_complete_deterministic_zipinfo_metadata() -> None:
    builder = runpy.run_path(str(BUNDLE_BUILDER))
    with ZipFile(ACTUAL_WHEEL) as wheel:
        assert wheel.comment == b""
        for info in wheel.infolist():
            expected_mode = 0o100644 if info.filename.startswith("hermes_cloud/") else 0o644
            assert info.date_time == (2026, 8, 2, 0, 0, 0)
            assert info.compress_type == 8
            assert info.create_system == 3
            assert info.create_version == 20
            assert info.extract_version == 20
            assert info.flag_bits == 0
            assert info.volume == 0
            assert info.internal_attr == 0
            assert info.external_attr == expected_mode << 16
            assert info.extra == b""
            assert info.comment == b""
    builder["_validate_wheel"](ACTUAL_WHEEL)


def test_sqlite_release_builder_rejects_a_wheel_from_an_unpinned_generator(
    tmp_path: Path,
) -> None:
    wheel = _mutated_actual_wheel(
        tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl",
        lambda payloads: payloads.__setitem__(
            "hermes_cloud-0.1.0.dist-info/WHEEL",
            payloads["hermes_cloud-0.1.0.dist-info/WHEEL"].replace(
                b"Generator: hatchling 1.31.0",
                b"Generator: hatchling 1.31.1",
            ),
        ),
    )

    completed = _run_builder(wheel, tmp_path / "candidate.tar.gz")

    assert completed.returncode != 0
    assert "candidate wheel rejected" in completed.stderr


def test_unpacked_sqlite_bundle_validates_exact_reviewed_systemd_units(
    tmp_path: Path,
) -> None:
    with tarfile.open(ACTUAL_BUNDLE, "r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    release = tmp_path / "hermes-cloud"
    validate = release / "deploy/test_server/scripts/validate.sh"
    release_python = release / ".venv/bin/python"
    release_python.parent.mkdir(parents=True)
    release_python.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = -I ] && [ "$2" = -c ]; then '
        f'exec {shlex.quote(sys.executable)} "$@"; fi\n'
        "exit 0\n"
    )
    release_python.chmod(0o700)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    capture = tmp_path / "systemd-capture"
    fake_systemd = fake_bin / "systemd-analyze"
    fake_systemd.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$SYSTEMD_CAPTURE"\n'
        "shift\n"
        'for unit in "$@"; do [ -f "$unit" ] || exit 44; done\n'
    )
    fake_systemd.chmod(0o700)

    completed = subprocess.run(
        ("bash", str(validate), "--systemd"),
        cwd=release,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SYSTEMD_CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    expected = [
        "verify",
        *(
            str(
                (
                    release
                    / "deploy/test_server/sqlite/systemd"
                    / f"hermes-cloud-sqlite-{service}.service"
                ).resolve()
            )
            for service in (
                "business-api",
                "connector-gateway",
                "migrate",
                "mint-connector-token",
                "seed-test-data",
            )
        ),
    ]
    assert completed.returncode == 0, completed.stderr
    assert capture.read_text().splitlines() == expected

    sqlite_systemd = release / "deploy/test_server/sqlite/systemd"
    for unit in sqlite_systemd.glob("*.service"):
        unit.unlink()
    empty = subprocess.run(
        ("bash", str(validate), "--systemd"),
        cwd=release,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SYSTEMD_CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty.returncode == 78
    assert "SQLite systemd unit set is incomplete" in empty.stderr

    sqlite_systemd.rmdir()
    missing = subprocess.run(
        ("bash", str(validate), "--systemd"),
        cwd=release,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SYSTEMD_CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 78
    assert "systemd unit directory is missing" in missing.stderr


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payloads: payloads.__setitem__(
            "hermes_cloud/stale_connector_runtime.py",
            b"STALE = True\n",
        ),
        lambda payloads: payloads.pop("hermes_cloud/platform/sqlite/README.md"),
        lambda payloads: payloads.__setitem__(
            "hermes_cloud-0.1.0.dist-info/entry_points.txt",
            b"[console_scripts]\nstale=hermes_cloud.stale:main\n",
        ),
        lambda payloads: payloads.__setitem__(
            "hermes_cloud-0.1.0.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: stale-cloud\nVersion: 9.9.9\n",
        ),
    ),
)
def test_sqlite_release_builder_rejects_wheel_payload_drift(
    tmp_path: Path,
    mutate: Callable[[dict[str, bytes]], None],
) -> None:
    wheel = _mutated_actual_wheel(
        tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl",
        mutate,
    )
    completed = _run_builder(wheel, tmp_path / "candidate.tar.gz")

    assert completed.returncode != 0
    assert "candidate wheel rejected" in completed.stderr


@pytest.mark.parametrize(
    ("mutate", "case"),
    (
        (
            lambda metadata: metadata.replace_header("Requires-Python", ">=3.12"),
            "requires-python",
        ),
        (
            lambda metadata: metadata.__delitem__("Requires-Dist"),
            "deleted-dependencies",
        ),
        (
            lambda metadata: metadata.__setitem__(
                "Requires-Dist", "unreviewed-package>=1"
            ),
            "added-dependency",
        ),
        (
            lambda metadata: metadata.replace_header("License", "Proprietary"),
            "license",
        ),
        (
            lambda metadata: metadata.replace_header("Summary", "Changed summary"),
            "summary",
        ),
        (
            lambda metadata: metadata.__setitem__("X-Unreviewed", "true"),
            "unreviewed-header",
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_sqlite_release_builder_rejects_unreviewed_metadata(
    tmp_path: Path,
    mutate: Callable[[EmailMessage], None],
    case: str,
) -> None:
    del case
    wheel = _mutated_actual_wheel(
        tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl",
        lambda payloads: _mutate_metadata(payloads, mutate),
    )

    completed = _run_builder(wheel, tmp_path / "candidate.tar.gz")

    assert completed.returncode != 0
    assert "candidate wheel rejected" in completed.stderr


def test_sqlite_release_builder_accepts_semantic_metadata_reordering(
    tmp_path: Path,
) -> None:
    def reorder_dependencies(metadata: EmailMessage) -> None:
        dependencies = metadata.get_all("Requires-Dist", [])
        del metadata["Requires-Dist"]
        for dependency in reversed(dependencies):
            metadata["Requires-Dist"] = dependency

    wheel = _mutated_actual_wheel(
        tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl",
        lambda payloads: _mutate_metadata(payloads, reorder_dependencies),
    )

    completed = _run_builder(wheel, tmp_path / "candidate.tar.gz")

    assert completed.returncode == 0, completed.stderr


def test_sqlite_release_builder_rejects_a_wheel_containing_tests(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_cloud/__init__.py", "")
        archive.writestr("hermes_cloud/tests/test_leak.py", "")
    output = tmp_path / "candidate.tar.gz"

    completed = _run_builder(wheel, output)

    assert completed.returncode != 0
    assert not output.exists()
    assert "candidate wheel rejected" in completed.stderr
    assert str(wheel) not in completed.stderr


@pytest.mark.parametrize(
    "member",
    (
        "hermes_cloud/__pycache__/module.pyc",
        "hermes_cloud/._module.py",
        "hermes_cloud/.DS_Store",
        "hermes_cloud/secrets/runtime_database_dsn",
        "hermes_cloud/runtime.sqlite3",
    ),
)
def test_sqlite_release_builder_rejects_sensitive_or_generated_wheel_members(
    tmp_path: Path,
    member: str,
) -> None:
    wheel = tmp_path / "hermes_cloud-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_cloud/__init__.py", "")
        archive.writestr(member, "not-a-real-secret")

    completed = _run_builder(wheel, tmp_path / "candidate.tar.gz")

    assert completed.returncode != 0
    assert "candidate wheel rejected" in completed.stderr
