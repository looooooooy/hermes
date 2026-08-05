"""Wheel and sdist fixtures for isolated packaging tests."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
UPGRADE_MODULE_PATH = PLUGIN_ROOT / "packaging/common/upgrade_distribution.py"


def _write_fixture_wheel(
    output_directory: Path,
    *,
    distribution: str,
    version: str,
    package_files: dict[str, str],
    entry_points: dict[str, str] | None = None,
    requirements: tuple[str, ...] = (),
) -> Path:
    normalized_distribution = re.sub(r"[-_.]+", "_", distribution)
    distribution_info = f"{normalized_distribution}-{version}.dist-info"
    wheel_path = (
        output_directory / f"{normalized_distribution}-{version}-py3-none-any.whl"
    )
    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {distribution}",
        f"Version: {version}",
    ]
    metadata_lines.extend(
        f"Requires-Dist: {requirement}" for requirement in requirements
    )
    files = {
        **package_files,
        f"{distribution_info}/METADATA": "\n".join(metadata_lines) + "\n",
        f"{distribution_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: hermes-agent-plugin-tests\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    if entry_points is not None:
        files[f"{distribution_info}/entry_points.txt"] = (
            "[hermes_agent.plugins]\n"
            + "".join(f"{name} = {value}\n" for name, value in entry_points.items())
        )
    record_path = f"{distribution_info}/RECORD"
    files[record_path] = "".join(f"{path},,\n" for path in (*files, record_path))

    with zipfile.ZipFile(
        wheel_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as wheel:
        for path, content in files.items():
            wheel.writestr(path, content)

    return wheel_path


@pytest.fixture(scope="session")
def canonical_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output_directory = tmp_path_factory.mktemp("canonical-wheel")
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(output_directory),
        ],
        cwd=PLUGIN_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output_directory.glob("hermes_agent_plugin-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="session")
def canonical_sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output_directory = tmp_path_factory.mktemp("canonical-sdist")
    subprocess.run(
        [
            "uv",
            "build",
            "--sdist",
            "--no-sources",
            "--out-dir",
            str(output_directory),
        ],
        cwd=PLUGIN_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sdists = list(output_directory.glob("hermes_agent_plugin-*.tar.gz"))
    assert len(sdists) == 1
    return sdists[0]


@pytest.fixture(scope="session")
def runtime_dependency_wheels(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, ...]:
    output_directory = tmp_path_factory.mktemp("runtime-dependencies")
    hermes_agent = _write_fixture_wheel(
        output_directory,
        distribution="hermes-agent",
        version="0.19.0",
        package_files={
            "hermes_agent_runtime/__init__.py": (
                '"""Hermes Agent runtime fixture."""\n'
            )
        },
    )
    websockets = _write_fixture_wheel(
        output_directory,
        distribution="websockets",
        version="13.0",
        package_files={"websockets/__init__.py": '__version__ = "13.0"\n'},
    )
    return hermes_agent, websockets


@pytest.fixture(scope="session")
def legacy_wheel_factory(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[str], Path]:
    def build(version: str = "0.0.9") -> Path:
        output_directory = tmp_path_factory.mktemp(f"legacy-wheel-{version}")
        return _write_fixture_wheel(
            output_directory,
            distribution="hermes-mobile-gateway",
            version=version,
            package_files={
                "hermes_mobile_gateway/__init__.py": (
                    '"""Legacy distribution fixture."""\n'
                    "\n"
                    "LEGACY_DISTRIBUTION = True\n"
                    "\n"
                    "def register(context):\n"
                    "    context.register_gateway_extension(object())\n"
                ),
                "hermes_mobile_gateway/extension.py": (
                    "class HermesMobileGatewayExtension:\n    pass\n"
                ),
                "hermes_mobile_gateway/control_contract.py": (
                    "CONTROL_CONTRACT_VERSION = 1\n"
                ),
            },
            entry_points={"hermes-mobile-gateway": "hermes_mobile_gateway"},
            requirements=(
                "hermes-agent>=0.19,<0.21",
                "websockets>=13,<17",
            ),
        )

    return build


@pytest.fixture(scope="session")
def legacy_wheel(
    legacy_wheel_factory: Callable[[str], Path],
) -> Path:
    return legacy_wheel_factory("0.0.9")


@pytest.fixture
def canonical_variant_factory(
    tmp_path: Path,
    canonical_wheel: Path,
) -> Callable[[str], Path]:
    def build(additional_requirement: str) -> Path:
        output_directory = tmp_path / re.sub(
            r"[^a-zA-Z0-9]+",
            "-",
            additional_requirement,
        )
        output_directory.mkdir()
        output_path = output_directory / canonical_wheel.name
        with zipfile.ZipFile(canonical_wheel) as source:
            files = {name: source.read(name) for name in source.namelist()}
        metadata_path = next(
            name for name in files if name.endswith(".dist-info/METADATA")
        )
        header, separator, body = files[metadata_path].decode().partition("\n\n")
        files[metadata_path] = (
            f"{header}\nRequires-Dist: {additional_requirement}{separator}{body}"
        ).encode()
        record_path = next(name for name in files if name.endswith(".dist-info/RECORD"))
        files[record_path] = "".join(f"{name},,\n" for name in files).encode()
        with zipfile.ZipFile(
            output_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as candidate:
            for name, content in files.items():
                candidate.writestr(name, content)
        return output_path

    return build


@pytest.fixture
def plugin_wheel_factory(
    tmp_path: Path,
) -> Callable[[str, str], Path]:
    def build(distribution: str, entry_point_name: str) -> Path:
        module_name = re.sub(r"[-.]+", "_", distribution)
        output_directory = tmp_path / module_name
        output_directory.mkdir()
        return _write_fixture_wheel(
            output_directory,
            distribution=distribution,
            version="1.0.0",
            package_files={
                f"{module_name}/__init__.py": (f'PLUGIN_NAME = "{entry_point_name}"\n')
            },
            entry_points={entry_point_name: module_name},
        )

    return build


@pytest.fixture
def upgrade_module():
    assert UPGRADE_MODULE_PATH.is_file()
    spec = importlib.util.spec_from_file_location(
        "hermes_distribution_upgrade",
        UPGRADE_MODULE_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    module_directory = str(UPGRADE_MODULE_PATH.parent)
    sys.path.insert(0, module_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(module_directory)
    return module


@pytest.fixture
def upgrade_internals(upgrade_module):
    """Expose mutation helpers only to packaging tests."""
    from upgrade import environment, transaction

    return SimpleNamespace(
        initialize_legacy_environment=(transaction.initialize_legacy_environment),
        install_bundle_wheels=environment.install_bundle_wheels,
        uninstall_distribution=environment.uninstall_distribution,
        transaction=transaction,
    )
