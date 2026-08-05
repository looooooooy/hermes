"""Opt-in contract probe against the user's current Hermes source and venv."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

_LIVE_SOURCE_ENV = "HERMES_LIVE_SOURCE_ROOT"
_LIVE_PROCESS_ENV = "HERMES_LIVE_PROCESS_ID"
_LIVE_EXECUTABLE_ENV = "HERMES_LIVE_PROCESS_EXECUTABLE"
_REQUIRED_CONTEXT_MEMBERS = (
    "gateway_extension_spi_version",
    "gateway_extension_capabilities",
    "register_gateway_extension",
)
_SAFE_PROBE = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    import hermes_agent_plugin
    import hermes_cli
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    source_root = Path(sys.argv[1]).resolve()
    context = PluginContext(
        PluginManifest(
            name="hermes-agent-plugin",
            source="entrypoint",
            key="hermes-agent-plugin",
        ),
        PluginManager(),
    )
    missing = [
        name for name in (
            "gateway_extension_spi_version",
            "gateway_extension_capabilities",
            "register_gateway_extension",
        )
        if not hasattr(context, name)
    ]
    fail_closed = False
    error_type = ""
    try:
        hermes_agent_plugin.register(context)
    except hermes_agent_plugin.HermesHostCompatibilityError as error:
        fail_closed = True
        error_type = type(error).__name__
    print(json.dumps({
        "hermes_version": hermes_cli.__version__,
        "source_matches": Path(hermes_cli.__file__).resolve().is_relative_to(source_root),
        "missing_context_members": missing,
        "fail_closed": fail_closed,
        "error_type": error_type,
    }, sort_keys=True))
    """
)


def test_current_live_hermes_019_fails_closed_without_creating_endpoints(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    if configured_root is None:
        pytest.skip(f"set {_LIVE_SOURCE_ENV} to run the live source contract")
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")

    hermes_home = tmp_path / "hermes-home"
    runtime_root = (
        Path("/tmp").resolve(strict=True) / f"hap-live-contract-{os.getpid()}"
    )
    role_directories = {
        "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": runtime_root / "local-registry",
        "HERMES_LOCAL_GATEWAY_SOCKET_DIR": runtime_root / "local-sockets",
        "HERMES_CONTROL_REGISTRY_DIR": runtime_root / "control-registry",
        "HERMES_CONTROL_SOCKET_DIR": runtime_root / "control-sockets",
        "HERMES_OBSERVER_REGISTRY_DIR": runtime_root / "observer-registry",
        "HERMES_OBSERVER_SOCKET_DIR": runtime_root / "observer-sockets",
    }
    environment = {
        "HERMES_HOME": str(hermes_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            (
                str(Path(__file__).resolve().parents[3] / "src"),
                str(source_root),
            )
        ),
        **{name: str(path) for name, path in role_directories.items()},
    }

    result = subprocess.run(
        [str(python), "-c", _SAFE_PROBE, str(source_root)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "error_type": "HermesHostCompatibilityError",
        "fail_closed": True,
        "hermes_version": "0.19.0",
        "missing_context_members": list(_REQUIRED_CONTEXT_MEMBERS),
        "source_matches": True,
    }
    assert hermes_home.exists() is False
    assert runtime_root.exists() is False
    assert all(path.exists() is False for path in role_directories.values())


def test_current_live_hermes_exposes_a_runnable_safe_diagnostic(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    if configured_root is None:
        pytest.skip(f"set {_LIVE_SOURCE_ENV} to run the live source contract")
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")

    real_home_parent = tmp_path / "real-home-parent"
    real_home_parent.mkdir()
    aliased_home_parent = tmp_path / "aliased-home-parent"
    aliased_home_parent.symlink_to(real_home_parent, target_is_directory=True)
    hermes_home = aliased_home_parent / "diagnostic-hermes-home"
    environment = {
        "HERMES_HOME": str(hermes_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            (
                str(Path(__file__).resolve().parents[3] / "src"),
                str(source_root),
            )
        ),
    }

    result = subprocess.run(
        [
            str(python),
            "-m",
            "hermes_agent_plugin.diagnostics",
            "--expected-source-root",
            str(source_root),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "action": "upgrade_running_hermes_agent_or_disable_plugin",
        "binding": "isolated_host_context_from_current_interpreter",
        "compatible": False,
        "core_patch_map": "docs/plans/hermes-core-host-spi-v1-patch-map.md",
        "host_distribution": "hermes-agent",
        "host_distribution_version": "0.19.0",
        "host_module": "hermes_cli",
        "host_source_root": str(source_root),
        "host_version": "0.19.0",
        "missing_context_members": sorted(_REQUIRED_CONTEXT_MEMBERS),
        "missing_required_capabilities": [],
        "observed_spi_version": None,
        "probe_scope": "compatibility_only_no_registration",
        "production_binding": "running_hermes_agent_plugin_manager",
        "next_step": "implement_gateway_extension_v1_with_explicit_authorization",
        "reason": "missing_context_members",
        "required_host_spi": "gateway-extension/1",
        "required_spi_version": 1,
        "source_matches": True,
    }
    assert hermes_home.exists() is False


def test_current_live_hermes_source_probe_rejects_a_different_source_root(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    if configured_root is None:
        pytest.skip(f"set {_LIVE_SOURCE_ENV} to run the live source contract")
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")
    different_source_root = tmp_path / "different-hermes-source"
    different_source_root.mkdir()

    result = subprocess.run(
        [
            str(python),
            "-m",
            "hermes_agent_plugin.diagnostics",
            "--expected-source-root",
            str(different_source_root),
        ],
        cwd=tmp_path,
        env=_live_diagnostic_environment(source_root, tmp_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["reason"] == "host_process_binding_mismatch"


def test_current_live_hermes_diagnostic_binds_the_explicit_running_process(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    configured_process = os.environ.get(_LIVE_PROCESS_ENV)
    configured_executable = os.environ.get(_LIVE_EXECUTABLE_ENV)
    if (
        configured_root is None
        or configured_process is None
        or configured_executable is None
    ):
        pytest.skip(
            f"set {_LIVE_SOURCE_ENV}, {_LIVE_PROCESS_ENV}, and "
            f"{_LIVE_EXECUTABLE_ENV} to run the live process contract"
        )
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")

    hermes_home = tmp_path / "diagnostic-hermes-home"
    environment = {
        "HERMES_HOME": str(hermes_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            (
                str(Path(__file__).resolve().parents[3] / "src"),
                str(source_root),
            )
        ),
    }

    result = subprocess.run(
        [
            str(python),
            "-m",
            "hermes_agent_plugin.diagnostics",
            "--expected-source-root",
            str(source_root),
            "--expected-host-pid",
            configured_process,
            "--expected-host-executable",
            configured_executable,
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["binding"] == "verified_live_process_installation"
    assert report["host_process_id"] == int(configured_process)
    assert report["host_process_executable"] == configured_executable
    assert report["host_process_matches"] is True
    assert report["host_process_launch"] == "python_module:hermes_cli.main/serve"
    assert report["host_process_parent_id"] > 1
    assert report["host_process_parent_executable"].startswith(
        str(source_root / "apps" / "desktop" / "release")
    )
    assert report["source_matches"] is True
    assert report["probe_scope"] == "compatibility_only_no_registration"
    assert report["production_binding"] == "running_hermes_agent_plugin_manager"
    assert report["compatible"] is False
    assert report["reason"] == "missing_context_members"
    assert hermes_home.exists() is False


def test_current_live_hermes_diagnostic_rejects_pid_one_even_if_executable_matches(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    if configured_root is None:
        pytest.skip(f"set {_LIVE_SOURCE_ENV} to run the live source contract")
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")
    pid_one_executable = subprocess.run(
        ["/bin/ps", "-p", "1", "-o", "comm="],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()

    result = subprocess.run(
        [
            str(python),
            "-m",
            "hermes_agent_plugin.diagnostics",
            "--expected-source-root",
            str(source_root),
            "--expected-host-pid",
            "1",
            "--expected-host-executable",
            pid_one_executable,
        ],
        cwd=tmp_path,
        env=_live_diagnostic_environment(source_root, tmp_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["reason"] == "host_process_binding_mismatch"


def test_current_live_hermes_diagnostic_rejects_a_different_source_root(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    configured_process = os.environ.get(_LIVE_PROCESS_ENV)
    configured_executable = os.environ.get(_LIVE_EXECUTABLE_ENV)
    if (
        configured_root is None
        or configured_process is None
        or configured_executable is None
    ):
        pytest.skip(
            f"set {_LIVE_SOURCE_ENV}, {_LIVE_PROCESS_ENV}, and "
            f"{_LIVE_EXECUTABLE_ENV} to run the live process contract"
        )
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")
    different_source_root = tmp_path / "different-hermes-source"
    different_source_root.mkdir()

    result = subprocess.run(
        [
            str(python),
            "-m",
            "hermes_agent_plugin.diagnostics",
            "--expected-source-root",
            str(different_source_root),
            "--expected-host-pid",
            configured_process,
            "--expected-host-executable",
            configured_executable,
        ],
        cwd=tmp_path,
        env=_live_diagnostic_environment(source_root, tmp_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["reason"] == "host_process_binding_mismatch"


def _live_diagnostic_environment(
    source_root: Path,
    tmp_path: Path,
) -> dict[str, str]:
    return {
        "HERMES_HOME": str(tmp_path / "diagnostic-hermes-home"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            (
                str(Path(__file__).resolve().parents[3] / "src"),
                str(source_root),
            )
        ),
    }


def test_current_live_hermes_diagnostic_rejects_a_different_process_executable(
    tmp_path: Path,
) -> None:
    configured_root = os.environ.get(_LIVE_SOURCE_ENV)
    configured_process = os.environ.get(_LIVE_PROCESS_ENV)
    configured_executable = os.environ.get(_LIVE_EXECUTABLE_ENV)
    if (
        configured_root is None
        or configured_process is None
        or configured_executable is None
    ):
        pytest.skip(
            f"set {_LIVE_SOURCE_ENV}, {_LIVE_PROCESS_ENV}, and "
            f"{_LIVE_EXECUTABLE_ENV} to run the live process contract"
        )
    source_root = Path(configured_root).resolve()
    python = source_root / "venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("configured live Hermes venv is unavailable")

    hermes_home = tmp_path / "diagnostic-hermes-home"
    environment = {
        "HERMES_HOME": str(hermes_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            (
                str(Path(__file__).resolve().parents[3] / "src"),
                str(source_root),
            )
        ),
    }

    result = subprocess.run(
        [
            str(python),
            "-m",
            "hermes_agent_plugin.diagnostics",
            "--expected-source-root",
            str(source_root),
            "--expected-host-pid",
            configured_process,
            "--expected-host-executable",
            configured_executable + "-different",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 3, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "action": "verify_running_hermes_agent_installation",
        "binding": "unbound_host_probe",
        "compatible": False,
        "core_patch_map": "docs/plans/hermes-core-host-spi-v1-patch-map.md",
        "next_step": "verify_running_hermes_agent_installation",
        "probe_scope": "compatibility_only_no_registration",
        "production_binding": "running_hermes_agent_plugin_manager",
        "reason": "host_process_binding_mismatch",
        "required_host_spi": "gateway-extension/1",
        "required_spi_version": 1,
    }
    assert hermes_home.exists() is False
