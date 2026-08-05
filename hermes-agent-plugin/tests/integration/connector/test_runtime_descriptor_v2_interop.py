"""Fresh-wheel Plugin writer interoperability with public Connector APIs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_ROOT = PLUGIN_ROOT.parent
CONNECTOR_ROOT = REPOSITORY_ROOT / "hermes-connector"
HARNESS = Path(__file__).with_name("wheel_runtime_descriptor_v2_harness.py")
DESCRIPTOR_FIXTURE = (
    REPOSITORY_ROOT
    / "contracts"
    / "fixtures"
    / "valid"
    / "local-runtime-discovery-descriptor-v2.json"
)


def _run_checked(command: list[str], *, timeout: float = 120.0) -> None:
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def isolated_wheel_python(tmp_path_factory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required for isolated wheel interoperability")
    root = tmp_path_factory.mktemp("runtime-v2-wheels")
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    _run_checked(
        [uv, "build", "--wheel", "--out-dir", str(wheelhouse), str(PLUGIN_ROOT)]
    )
    _run_checked(
        [uv, "build", "--wheel", "--out-dir", str(wheelhouse), str(CONNECTOR_ROOT)]
    )
    plugin_wheels = tuple(wheelhouse.glob("hermes_agent_plugin-*.whl"))
    connector_wheels = tuple(wheelhouse.glob("hermes_connector-*.whl"))
    assert len(plugin_wheels) == 1
    assert len(connector_wheels) == 1

    environment = root / "environment"
    _run_checked([uv, "venv", "--python", sys.executable, str(environment)])
    python = environment / "bin" / "python"
    _run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(plugin_wheels[0]),
            str(connector_wheels[0]),
        ]
    )
    return python


def _run_harness(python: Path, mutation: str) -> dict[str, object]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="hap-wheel-v2-", dir="/tmp") as raw:
        completed = subprocess.run(
            [python, str(HARNESS), raw, mutation],
            cwd=raw,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    return json.loads(completed.stdout)


def test_actual_plugin_and_connector_wheels_interoperate_through_public_apis(
    isolated_wheel_python: Path,
) -> None:
    result = _run_harness(isolated_wheel_python, "none")
    expected_fields = sorted(json.loads(DESCRIPTOR_FIXTURE.read_text(encoding="utf-8")))

    assert result["result"] == {
        "local": True,
        "control": True,
        "observer": True,
    }
    assert result["installations"] == {
        "plugin": {
            "editable": False,
            "wheel_url": True,
            "inside_environment": True,
        },
        "connector": {
            "editable": False,
            "wheel_url": True,
            "inside_environment": True,
        },
    }
    assert len(result["descriptors"]) == 3
    for descriptor in result["descriptors"]:
        assert descriptor["fields"] == expected_fields
        assert descriptor["version"] == 2
        assert descriptor["mode"] == 0o600
        assert descriptor["socket_inode"] > 0


@pytest.mark.parametrize(
    "mutation",
    ("v1", "missing-field", "process-evidence-mismatch"),
)
def test_public_connector_apis_reject_nonexact_wheel_descriptors(
    isolated_wheel_python: Path,
    mutation: str,
) -> None:
    result = _run_harness(isolated_wheel_python, mutation)

    assert result["result"] == {
        "local": False,
        "control": False,
        "observer": False,
    }
