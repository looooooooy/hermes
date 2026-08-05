from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest

ENTRYPOINT_NAMES = (
    "business_api",
    "connector_gateway",
    "worker",
    "file_gateway",
)
REQUIRED_ENTRYPOINT_FILES = frozenset(
    f"hermes_cloud/entrypoints/{entrypoint_name}/{filename}"
    for entrypoint_name in ENTRYPOINT_NAMES
    for filename in ("__init__.py", "app.py", "bootstrap.py")
)
ENTRYPOINTS_PACKAGE = PurePosixPath("hermes_cloud/entrypoints")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _distribution_layout_violations(
    names: frozenset[PurePosixPath],
) -> tuple[str, ...]:
    violations = {
        f"missing {required}"
        for required in REQUIRED_ENTRYPOINT_FILES
        if PurePosixPath(required) not in names
    }
    flat_modules = {
        ENTRYPOINTS_PACKAGE / f"{entrypoint_name}.py"
        for entrypoint_name in ENTRYPOINT_NAMES
    }
    for path in names:
        if path in flat_modules or path.suffix == ".pyc" or "__pycache__" in path.parts:
            violations.add(f"forbidden {path.as_posix()}")
    return tuple(sorted(violations))


def _normalized_name(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if "hermes_cloud" not in path.parts:
        return None
    start = path.parts.index("hermes_cloud")
    return PurePosixPath(*path.parts[start:])


def _wheel_names(path: Path) -> frozenset[PurePosixPath]:
    with ZipFile(path) as archive:
        return frozenset(
            normalized
            for name in archive.namelist()
            if (normalized := _normalized_name(name)) is not None
        )


def _sdist_names(path: Path) -> frozenset[PurePosixPath]:
    with tarfile.open(path, "r:gz") as archive:
        return frozenset(
            normalized
            for member in archive.getmembers()
            if (normalized := _normalized_name(member.name)) is not None
        )


@pytest.fixture(scope="module")
def fresh_entrypoint_artifacts(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    artifact_root = tmp_path_factory.mktemp("fresh-entrypoint-dist")
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        (uv, "build", "--offline", "--out-dir", str(artifact_root)),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return artifact_root


def test_distribution_layout_mutation_rejects_missing_and_generated_files() -> None:
    names = set(REQUIRED_ENTRYPOINT_FILES)
    missing = next(iter(REQUIRED_ENTRYPOINT_FILES))
    names.remove(missing)
    names.update(
        {
            "hermes_cloud/entrypoints/worker.py",
            "hermes_cloud/entrypoints/worker.pyc",
            "hermes_cloud/entrypoints/worker/__pycache__/app.pyc",
        }
    )
    checker = globals().get("_distribution_layout_violations")

    assert checker is not None
    violations = checker(frozenset(PurePosixPath(name) for name in names))
    assert f"missing {missing}" in violations
    assert "forbidden hermes_cloud/entrypoints/worker.py" in violations
    assert "forbidden hermes_cloud/entrypoints/worker.pyc" in violations
    assert "forbidden hermes_cloud/entrypoints/worker/__pycache__/app.pyc" in violations


def test_fresh_wheel_has_only_packaged_entrypoint_layout(
    fresh_entrypoint_artifacts: Path,
) -> None:
    wheels = tuple(fresh_entrypoint_artifacts.glob("*.whl"))

    assert len(wheels) == 1
    assert _distribution_layout_violations(_wheel_names(wheels[0])) == ()


def test_fresh_sdist_has_only_packaged_entrypoint_layout(
    fresh_entrypoint_artifacts: Path,
) -> None:
    sdists = tuple(fresh_entrypoint_artifacts.glob("*.tar.gz"))

    assert len(sdists) == 1
    assert _distribution_layout_violations(_sdist_names(sdists[0])) == ()


def test_fresh_wheel_installs_with_exact_public_entrypoint_api(
    fresh_entrypoint_artifacts: Path,
    tmp_path: Path,
) -> None:
    wheel = next(fresh_entrypoint_artifacts.glob("*.whl"))
    venv_root = tmp_path / "venv"
    subprocess.run(
        (sys.executable, "-m", "venv", str(venv_root)),
        check=True,
        capture_output=True,
        text=True,
    )
    isolated_python = venv_root / "bin" / "python"
    subprocess.run(
        (
            str(isolated_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--no-deps",
            str(wheel),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    verification = """
import importlib

expected = {
    "business_api": {"app", "create_app"},
    "connector_gateway": {
        "ConnectorGatewayApplication",
        "app",
        "create_app",
        "decode_connector_frame",
    },
    "worker": {"create_worker", "worker"},
    "file_gateway": {"app", "create_app"},
}
modules = {
    name: importlib.import_module(f"hermes_cloud.entrypoints.{name}")
    for name in expected
}
for name, module in modules.items():
    importlib.import_module(f"hermes_cloud.entrypoints.{name}.app")
    importlib.import_module(f"hermes_cloud.entrypoints.{name}.bootstrap")
    assert set(module.__all__) == expected[name]
    assert all(hasattr(module, public) for public in module.__all__)
    assert {
        public for public in vars(module) if not public.startswith("_")
    } == expected[name]

business = modules["business_api"]
assert business.app.snapshot()["component"] == "business-api"
assert business.create_app().snapshot()["component"] == "business-api"

connector = modules["connector_gateway"]
assert connector.app.snapshot()["component"] == "connector-gateway"
assert connector.create_app().__class__ is connector.ConnectorGatewayApplication
assert callable(connector.decode_connector_frame)

file_gateway = modules["file_gateway"]
assert file_gateway.app.snapshot()["component"] == "file-gateway"
assert file_gateway.create_app().snapshot()["component"] == "file-gateway"

worker = modules["worker"]
assert worker.worker.snapshot()["component"] == "async-worker"
assert worker.create_worker().snapshot()["component"] == "async-worker"
"""
    subprocess.run(
        (str(isolated_python), "-I", "-c", verification),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
