"""Source-distribution upgrade tooling and standalone CLI tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

RETIRED_IMPORT_SEGMENTS = ("hermes", "mobile", "gateway")
RETIRED_IMPORT_PACKAGE = "_".join(RETIRED_IMPORT_SEGMENTS)
RETIRED_IDENTIFIER_ALLOWED_PREFIXES = (
    "packaging/common/",
    "tests/packaging/conftest.py",
    "tests/packaging/test_legacy_upgrade.py",
    "tests/packaging/test_upgrade_",
)


def _python_path(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts/python.exe"
    return environment / "bin/python"


def _extract_upgrade_tooling(
    canonical_sdist: Path,
    destination: Path,
) -> tuple[Path, Path, set[str]]:
    with tarfile.open(canonical_sdist, "r:gz") as archive:
        entries = set(archive.getnames())
        root = next(
            entry.split("/", 1)[0]
            for entry in entries
            if entry.endswith("/pyproject.toml")
        )
        common_prefix = f"{root}/packaging/common/"
        tooling_members = [
            name
            for name in entries
            if name.startswith(common_prefix)
            and name.endswith((".py", "legacy-to-canonical.json"))
        ]
        for member_name in tooling_members:
            member = archive.extractfile(member_name)
            assert member is not None
            relative_path = Path(member_name.removeprefix(common_prefix))
            output_path = destination / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(member.read())
    return (
        destination / "upgrade_distribution.py",
        destination / "legacy-to-canonical.json",
        entries,
    )


def test_sdist_contains_external_upgrade_rules_and_standalone_cli(
    tmp_path: Path,
    canonical_sdist: Path,
) -> None:
    script_path, rules_path, entries = _extract_upgrade_tooling(
        canonical_sdist,
        tmp_path,
    )

    assert any(
        entry.endswith("/packaging/common/upgrade/models.py") for entry in entries
    )
    assert any(
        entry.endswith("/packaging/common/upgrade/transaction.py") for entry in entries
    )
    help_result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    for command in ("upgrade", "rollback", "inspect"):
        assert command in help_result.stdout
    rules_value = json.loads(rules_path.read_text(encoding="utf-8"))
    assert rules_value["included_in_plugin_wheel"] is False


def test_sdist_excludes_the_retired_legacy_import_package(
    canonical_sdist: Path,
) -> None:
    with tarfile.open(canonical_sdist, "r:gz") as archive:
        entries = set(archive.getnames())

        assert not any(f"/src/{RETIRED_IMPORT_PACKAGE}/" in entry for entry in entries)
        assert not any("/tests/" in entry for entry in entries)
        assert not any("test_support" in entry for entry in entries)


def test_sdist_mentions_retired_identity_only_in_upgrade_transaction_material(
    canonical_sdist: Path,
) -> None:
    unexpected: list[str] = []
    needle = RETIRED_IMPORT_PACKAGE.encode()
    with tarfile.open(canonical_sdist, "r:gz") as archive:
        for entry in archive.getmembers():
            if not entry.isfile():
                continue
            member = archive.extractfile(entry)
            assert member is not None
            if needle not in member.read():
                continue
            relative = entry.name.split("/", 1)[1]
            if not relative.startswith(RETIRED_IDENTIFIER_ALLOWED_PREFIXES):
                unexpected.append(relative)

    assert unexpected == []


def test_extracted_sdist_cli_upgrades_without_canonical_preinstalled(
    tmp_path: Path,
    canonical_sdist: Path,
    canonical_wheel: Path,
    legacy_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
    upgrade_module,
    upgrade_internals,
) -> None:
    script_path, _, _ = _extract_upgrade_tooling(
        canonical_sdist,
        tmp_path / "sdist-tooling",
    )
    environment = tmp_path / "extension-environment"
    upgrade_internals.initialize_legacy_environment(
        environment,
        legacy_wheel,
        bundle_wheels=runtime_dependency_wheels,
    )
    python = _python_path(environment)

    result = subprocess.run(
        [
            str(python),
            str(script_path),
            "upgrade",
            "--environment",
            str(environment),
            "--legacy-wheel",
            str(legacy_wheel),
            "--canonical-wheel",
            str(canonical_wheel),
            "--transaction-directory",
            str(tmp_path / "transaction"),
            "--host-stopped",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    receipt_summary = json.loads(result.stdout)
    assert receipt_summary["status"] == "completed"
    assert upgrade_module.inspect_environment(environment)["canonical"] == ("0.1.0")
