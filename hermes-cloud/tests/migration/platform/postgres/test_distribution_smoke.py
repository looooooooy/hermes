from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SOURCE_PACKAGE = PROJECT_ROOT / "src" / "hermes_cloud"
REQUIRED_MODULES = (
    "hermes_cloud/platform/postgres/catalog.py",
    "hermes_cloud/platform/postgres/ddl.py",
    "hermes_cloud/platform/postgres/models.py",
    "hermes_cloud/platform/postgres/session.py",
)
COMPATIBILITY_MODULES = (
    "hermes_cloud/adapters/postgres_models.py",
    "hermes_cloud/adapters/postgres_v1.py",
)
FORBIDDEN_PACKAGED_MARKERS = (
    b"execute_script",
    b"exec_driver_sql",
    b"Migration.sql",
    b"_FOUNDATION_SQL",
    b"_TENANT_STORAGE_SQL",
    b"_RUNTIME_ROLE_SQL",
    b"_FOUNDATION_GAP_TABLES_SQL",
)


@pytest.fixture(scope="module")
def fresh_artifact_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    artifact_root = tmp_path_factory.mktemp("fresh-hermes-cloud-dist")
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        (uv, "build", "--out-dir", str(artifact_root)),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return artifact_root


def _normalized_name(name: str) -> str | None:
    path = PurePosixPath(name)
    parts = path.parts
    if "hermes_cloud" not in parts:
        return None
    start = parts.index("hermes_cloud")
    return PurePosixPath(*parts[start:]).as_posix()


def _wheel_sources(path: Path) -> dict[str, bytes]:
    with ZipFile(path) as archive:
        return {
            normalized: archive.read(name)
            for name in archive.namelist()
            if (normalized := _normalized_name(name)) is not None
            and normalized.endswith(".py")
        }


def _sdist_sources(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as archive:
        sources: dict[str, bytes] = {}
        for member in archive.getmembers():
            normalized = _normalized_name(member.name)
            if normalized is None or not normalized.endswith(".py"):
                continue
            extracted = archive.extractfile(member)
            assert extracted is not None
            sources[normalized] = extracted.read()
        return sources


def _workspace_sources() -> dict[str, bytes]:
    return {
        path.relative_to(SOURCE_PACKAGE.parent).as_posix(): path.read_bytes()
        for path in SOURCE_PACKAGE.rglob("*.py")
    }


def _assert_thin_reexport(source: bytes) -> None:
    tree = ast.parse(source.decode("utf-8"))
    allowed = (
        ast.Expr,
        ast.ImportFrom,
        ast.Assign,
    )
    assert all(isinstance(node, allowed) for node in tree.body)
    imports = [node.module for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert imports
    assert all(
        module is not None and module.startswith("hermes_cloud.platform.postgres")
        for module in imports
    )


def _assert_current_distribution(sources: dict[str, bytes]) -> None:
    assert set(REQUIRED_MODULES).issubset(sources)
    assert set(COMPATIBILITY_MODULES).issubset(sources)
    for source in sources.values():
        for marker in FORBIDDEN_PACKAGED_MARKERS:
            assert marker not in source
        tree = ast.parse(source.decode("utf-8"))
        assert all(
            not (
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"DDL", "text"}
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"exec_driver_sql", "execute_script"}
                )
            )
            for node in ast.walk(tree)
        )
    for module in COMPATIBILITY_MODULES:
        _assert_thin_reexport(sources[module])


def test_fresh_wheel_matches_the_complete_python_source_tree(
    fresh_artifact_root: Path,
) -> None:
    wheels = tuple(fresh_artifact_root.glob("*.whl"))
    assert len(wheels) == 1
    sources = _wheel_sources(wheels[0])
    assert sources == _workspace_sources()
    _assert_current_distribution(sources)


def test_fresh_sdist_matches_the_complete_python_source_tree(
    fresh_artifact_root: Path,
) -> None:
    sdists = tuple(fresh_artifact_root.glob("*.tar.gz"))
    assert len(sdists) == 1
    sources = _sdist_sources(sdists[0])
    assert sources == _workspace_sources()
    _assert_current_distribution(sources)


def test_fresh_wheel_imports_in_isolation_and_verifies_both_catalogs(
    fresh_artifact_root: Path,
) -> None:
    wheel = next(fresh_artifact_root.glob("*.whl"))
    verification = """
import sys
sys.path.insert(0, sys.argv[1])
import hermes_cloud
assert ".whl/" in hermes_cloud.__file__
from hermes_cloud.contracts.mobile_control import CONTROL_ERROR_CODES
from hermes_cloud.domain.migrations import (
    PUBLISHED_POSTGRES_MIGRATIONS,
    verify_published_migration_registry,
)
from hermes_cloud.platform.postgres.catalog import (
    migration_plan_for,
    verify_migration_catalog,
)
assert len(CONTROL_ERROR_CODES) == 18
verify_published_migration_registry()
verify_migration_catalog()
for migration in PUBLISHED_POSTGRES_MIGRATIONS:
    migration_plan_for(migration)
"""
    subprocess.run(
        (sys.executable, "-I", "-c", verification, str(wheel)),
        cwd=fresh_artifact_root,
        check=True,
        capture_output=True,
        text=True,
    )
