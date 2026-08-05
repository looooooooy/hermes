"""Safe diagnostics for the public Hermes Host SPI boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from hermes_agent_plugin.bootstrap.registration import REQUIRED_HOST_CAPABILITIES
from hermes_agent_plugin.diagnostics import diagnose_host_context

_DIAGNOSTIC_ARGUMENT_ERROR = {
    "action": "correct_diagnostic_arguments",
    "binding": "unbound_host_probe",
    "compatible": False,
    "core_patch_map": "docs/plans/hermes-core-host-spi-v1-patch-map.md",
    "next_step": "correct_diagnostic_arguments",
    "probe_scope": "compatibility_only_no_registration",
    "production_binding": "running_hermes_agent_plugin_manager",
    "reason": "diagnostic_arguments_invalid",
    "required_host_spi": "gateway-extension/1",
    "required_spi_version": 1,
}


class _MissingHostSpiContext:
    pass


class _CompatibleHostSpiContext:
    gateway_extension_spi_version = 1
    gateway_extension_capabilities = REQUIRED_HOST_CAPABILITIES

    def register_gateway_extension(self, extension, *, spi_version):
        raise AssertionError("diagnostics must not register or start an extension")


def test_diagnostics_report_missing_host_spi_without_side_effects() -> None:
    report = diagnose_host_context(_MissingHostSpiContext())

    assert report.as_safe_dict() == {
        "action": "upgrade_running_hermes_agent_or_disable_plugin",
        "binding": "provided_host_context",
        "compatible": False,
        "core_patch_map": "docs/plans/hermes-core-host-spi-v1-patch-map.md",
        "missing_context_members": [
            "gateway_extension_capabilities",
            "gateway_extension_spi_version",
            "register_gateway_extension",
        ],
        "missing_required_capabilities": [],
        "observed_spi_version": None,
        "next_step": "implement_gateway_extension_v1_with_explicit_authorization",
        "reason": "missing_context_members",
        "required_host_spi": "gateway-extension/1",
        "required_spi_version": 1,
    }


def test_diagnostics_only_inspects_a_compatible_host_context() -> None:
    report = diagnose_host_context(_CompatibleHostSpiContext())

    assert report.as_safe_dict() == {
        "action": "none",
        "binding": "provided_host_context",
        "compatible": True,
        "core_patch_map": "docs/plans/hermes-core-host-spi-v1-patch-map.md",
        "missing_context_members": [],
        "missing_required_capabilities": [],
        "observed_spi_version": 1,
        "next_step": "none",
        "reason": "compatible",
        "required_host_spi": "gateway-extension/1",
        "required_spi_version": 1,
    }


def test_diagnostics_cli_reports_safe_json_when_runtime_imports_fail(
    tmp_path: Path,
) -> None:
    plugin_root = Path(__file__).resolve().parents[3]
    import_guard = tmp_path / "import-guard"
    import_guard.mkdir()
    (import_guard / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import sys

            class RuntimeImportFailure:
                def find_spec(self, fullname, path=None, target=None):
                    blocked = (
                        "hermes_agent_plugin.adapters",
                        "hermes_agent_plugin.bootstrap",
                        "hermes_cli",
                    )
                    if any(
                        fullname == prefix or fullname.startswith(prefix + ".")
                        for prefix in blocked
                    ):
                        raise RuntimeError("blocked runtime import")
                    return None

            sys.meta_path.insert(0, RuntimeImportFailure())
            """
        ),
        encoding="utf-8",
    )
    environment = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(import_guard), str(plugin_root / "src"))),
    }

    result = subprocess.run(
        [sys.executable, "-m", "hermes_agent_plugin.diagnostics"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "action": "verify_running_hermes_agent_installation",
        "binding": "unbound_host_probe",
        "compatible": False,
        "core_patch_map": "docs/plans/hermes-core-host-spi-v1-patch-map.md",
        "next_step": "verify_running_hermes_agent_installation",
        "probe_scope": "compatibility_only_no_registration",
        "production_binding": "running_hermes_agent_plugin_manager",
        "reason": "host_probe_failed",
        "required_host_spi": "gateway-extension/1",
        "required_spi_version": 1,
    }


def test_diagnostics_cli_reports_unknown_arguments_without_echoing_them(
    tmp_path: Path,
) -> None:
    marker = "sensitive-unknown-value"

    result = _run_diagnostics_cli(tmp_path, ["--unknown-private-value", marker])

    assert result.returncode == 4
    assert result.stderr == ""
    assert marker not in result.stdout
    assert "unknown-private-value" not in result.stdout
    assert json.loads(result.stdout) == _DIAGNOSTIC_ARGUMENT_ERROR


def test_diagnostics_cli_reports_missing_argument_values_without_argparse_output(
    tmp_path: Path,
) -> None:
    result = _run_diagnostics_cli(tmp_path, ["--expected-host-pid"])

    assert result.returncode == 4
    assert result.stderr == ""
    assert json.loads(result.stdout) == _DIAGNOSTIC_ARGUMENT_ERROR


def test_diagnostics_cli_rejects_invalid_process_ids_without_echoing_them(
    tmp_path: Path,
) -> None:
    for invalid_process_id in ("not-a-pid", "0", "-1", "2147483648"):
        result = _run_diagnostics_cli(
            tmp_path,
            ["--expected-host-pid", invalid_process_id],
        )

        assert result.returncode == 4
        assert result.stderr == ""
        assert invalid_process_id not in result.stdout
        assert json.loads(result.stdout) == _DIAGNOSTIC_ARGUMENT_ERROR


def test_diagnostics_cli_rejects_incomplete_live_binding_arguments(
    tmp_path: Path,
) -> None:
    source_root = "/private/tmp/hermes-source"
    executable = f"{source_root}/venv/bin/python"
    invalid_combinations = (
        ["--expected-host-pid", "123"],
        ["--expected-host-executable", executable],
        [
            "--expected-host-pid",
            "123",
            "--expected-host-executable",
            executable,
        ],
        [
            "--expected-source-root",
            source_root,
            "--expected-host-pid",
            "123",
        ],
        [
            "--expected-source-root",
            source_root,
            "--expected-host-executable",
            executable,
        ],
    )

    for arguments in invalid_combinations:
        result = _run_diagnostics_cli(tmp_path, arguments)

        assert result.returncode == 4
        assert result.stderr == ""
        assert json.loads(result.stdout) == _DIAGNOSTIC_ARGUMENT_ERROR


def _run_diagnostics_cli(
    working_directory: Path,
    arguments: list[str],
) -> subprocess.CompletedProcess[str]:
    plugin_root = Path(__file__).resolve().parents[3]
    environment = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(plugin_root / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "hermes_agent_plugin.diagnostics", *arguments],
        cwd=working_directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
