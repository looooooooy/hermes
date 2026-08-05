"""Side-effect-free diagnostics for the public Hermes Host SPI boundary."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import distributions, packages_distributions
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from .host_compatibility import (
    HOST_SPI_VERSION,
    validate_host_context,
)

_HOST_SPI_CONTRACT = "gateway-extension/1"
_PROVIDED_CONTEXT_BINDING = "provided_host_context"
_PRODUCTION_BINDING = "running_hermes_agent_plugin_manager"
_ISOLATED_PROBE_BINDING = "isolated_host_context_from_current_interpreter"
_CORE_PATCH_MAP = "docs/plans/hermes-core-host-spi-v1-patch-map.md"
_PROBE_SCOPE = "compatibility_only_no_registration"
_ARGUMENT_ERROR_EXIT_CODE = 4
_MAX_PROCESS_ID = 2_147_483_647


class HostProcessBindingMismatch(RuntimeError):
    """Raised when an explicit process does not match the selected Host install."""


class DiagnosticArgumentError(RuntimeError):
    """Raised for unsafe or incomplete diagnostic command arguments."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DiagnosticArgumentError from None


@dataclass(frozen=True)
class _ProcessEvidence:
    process_id: int
    parent_process_id: int
    executable: str
    arguments: tuple[str, ...]
    parent_executable: str
    parent_arguments: tuple[str, ...]


@dataclass(frozen=True)
class HostCompatibilityReport:
    """Allowlisted compatibility facts that are safe to print or persist."""

    compatible: bool
    reason: str
    observed_spi_version: int | None
    missing_context_members: tuple[str, ...] = ()
    missing_required_capabilities: tuple[str, ...] = ()

    def as_safe_dict(self) -> dict[str, object]:
        """Return only stable facts; never include host values or exceptions."""
        return {
            "action": (
                "none"
                if self.compatible
                else "upgrade_running_hermes_agent_or_disable_plugin"
            ),
            "binding": _PROVIDED_CONTEXT_BINDING,
            "compatible": self.compatible,
            "core_patch_map": _CORE_PATCH_MAP,
            "missing_context_members": list(self.missing_context_members),
            "missing_required_capabilities": list(self.missing_required_capabilities),
            "observed_spi_version": self.observed_spi_version,
            "next_step": (
                "none"
                if self.compatible
                else "implement_gateway_extension_v1_with_explicit_authorization"
            ),
            "reason": self.reason,
            "required_host_spi": _HOST_SPI_CONTRACT,
            "required_spi_version": HOST_SPI_VERSION,
        }


def diagnose_host_context(context: Any) -> HostCompatibilityReport:
    """Inspect the frozen public SPI surface without registering an extension."""
    validation = validate_host_context(context)
    return HostCompatibilityReport(
        compatible=validation.compatible,
        reason=validation.reason,
        observed_spi_version=validation.observed_spi_version,
        missing_context_members=validation.missing_context_members,
        missing_required_capabilities=validation.missing_required_capabilities,
    )


def _process_field(process_id: int, field: str) -> str:
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(process_id), "-o", f"{field}="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={"LANG": os.environ.get("LANG", "C.UTF-8")},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HostProcessBindingMismatch from error
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise HostProcessBindingMismatch
    return value


def _process_arguments(process_id: int) -> tuple[str, ...]:
    try:
        arguments = tuple(shlex.split(_process_field(process_id, "command")))
    except ValueError as error:
        raise HostProcessBindingMismatch from error
    if not arguments:
        raise HostProcessBindingMismatch
    return arguments


def _running_process_evidence(process_id: int) -> _ProcessEvidence:
    try:
        parent_process_id = int(_process_field(process_id, "ppid"))
    except ValueError as error:
        raise HostProcessBindingMismatch from error
    if parent_process_id <= 1:
        raise HostProcessBindingMismatch
    executable = _process_field(process_id, "comm")
    parent_executable = _process_field(parent_process_id, "comm")
    if not Path(executable).is_absolute() or not Path(parent_executable).is_absolute():
        raise HostProcessBindingMismatch
    return _ProcessEvidence(
        process_id=process_id,
        parent_process_id=parent_process_id,
        executable=executable,
        arguments=_process_arguments(process_id),
        parent_executable=parent_executable,
        parent_arguments=_process_arguments(parent_process_id),
    )


def _distribution_source_root(host_distribution: Any, module_path: Path) -> Path:
    distribution_root = Path(host_distribution.locate_file("")).resolve(strict=True)
    for installed_file in host_distribution.files or ():
        if str(installed_file) == "hermes_cli/__init__.py":
            installed_module = Path(
                host_distribution.locate_file(installed_file)
            ).resolve(strict=True)
            if installed_module == module_path:
                return distribution_root
        if str(installed_file).endswith(".dist-info/direct_url.json"):
            try:
                metadata_path = Path(host_distribution.locate_file(installed_file))
                direct_url = json.loads(metadata_path.read_text(encoding="utf-8"))
                parsed_url = urlparse(direct_url["url"])
                if (
                    parsed_url.scheme != "file"
                    or parsed_url.netloc not in ("", "localhost")
                    or direct_url.get("dir_info", {}).get("editable") is not True
                ):
                    break
                return Path(unquote(parsed_url.path)).resolve(strict=True)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                break
    raise HostProcessBindingMismatch


def _validate_host_installation(
    hermes_cli: Any,
    expected_source_root: Path | None,
) -> tuple[Path, str]:
    module_path = Path(hermes_cli.__file__).resolve(strict=True)
    module_source_root = module_path.parent.parent
    host_version = str(getattr(hermes_cli, "__version__", "unknown"))
    module_distributions = packages_distributions().get("hermes_cli", ())
    host_distributions = tuple(distributions(name="hermes-agent"))
    matching_distributions = tuple(
        candidate
        for candidate in host_distributions
        if candidate.version == host_version
    )
    if "hermes-agent" not in module_distributions or not matching_distributions:
        raise HostProcessBindingMismatch
    if expected_source_root is not None:
        try:
            source_root = expected_source_root.resolve(strict=True)
            interpreter_root = Path(sys.prefix).resolve(strict=True)
        except OSError as error:
            raise HostProcessBindingMismatch from error
        distribution_matches = False
        for candidate in matching_distributions:
            try:
                distribution_matches = (
                    _distribution_source_root(candidate, module_path) == source_root
                )
            except (HostProcessBindingMismatch, OSError):
                continue
            if distribution_matches:
                break
        if (
            module_source_root != source_root
            or not distribution_matches
            or interpreter_root != (source_root / "venv").resolve(strict=True)
        ):
            raise HostProcessBindingMismatch
    return module_source_root, matching_distributions[0].version


def _desktop_parent_matches(source_root: Path, evidence: _ProcessEvidence) -> bool:
    try:
        relative_parent = Path(evidence.parent_executable).relative_to(
            source_root / "apps" / "desktop" / "release"
        )
    except ValueError:
        return False
    return (
        len(relative_parent.parts) == 5
        and relative_parent.parts[1:]
        == (
            "Hermes.app",
            "Contents",
            "MacOS",
            "Hermes",
        )
        and evidence.parent_arguments[0] == evidence.parent_executable
    )


def _backend_launch_kind(
    arguments: tuple[str, ...],
    expected_executable: str,
) -> str | None:
    if arguments[:3] != (expected_executable, "-m", "hermes_cli.main"):
        return None
    backend_arguments = arguments[3:]
    if backend_arguments[:1] == ("--profile",):
        if len(backend_arguments) < 3 or not backend_arguments[1]:
            return None
        backend_arguments = backend_arguments[2:]
    if backend_arguments == (
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ):
        return "python_module:hermes_cli.main/serve"
    if backend_arguments == (
        "dashboard",
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ):
        return "python_module:hermes_cli.main/dashboard"
    return None


def _validate_live_process_binding(
    source_root: Path,
    process_id: int,
    expected_executable: Path,
) -> tuple[_ProcessEvidence, str]:
    expected_executable_text = str(expected_executable.absolute())
    canonical_backend_executable = str(source_root / "venv" / "bin" / "python")
    if (
        expected_executable_text != canonical_backend_executable
        or not expected_executable.is_file()
    ):
        raise HostProcessBindingMismatch
    evidence = _running_process_evidence(process_id)
    launch_kind = _backend_launch_kind(evidence.arguments, expected_executable_text)
    if (
        evidence.executable != expected_executable_text
        or launch_kind is None
        or not _desktop_parent_matches(source_root, evidence)
    ):
        raise HostProcessBindingMismatch
    return evidence, launch_kind


def _positive_process_id(value: str) -> int:
    try:
        process_id = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError from error
    if process_id <= 0 or process_id > _MAX_PROCESS_ID:
        raise argparse.ArgumentTypeError
    return process_id


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError
    return path


def _validate_argument_combination(arguments: argparse.Namespace) -> None:
    live_values = (
        arguments.expected_host_pid,
        arguments.expected_host_executable,
    )
    if any(value is not None for value in live_values) and (
        arguments.expected_source_root is None
        or any(value is None for value in live_values)
    ):
        raise DiagnosticArgumentError


def _argument_error_payload() -> dict[str, object]:
    return {
        "action": "correct_diagnostic_arguments",
        "binding": "unbound_host_probe",
        "compatible": False,
        "core_patch_map": _CORE_PATCH_MAP,
        "next_step": "correct_diagnostic_arguments",
        "probe_scope": _PROBE_SCOPE,
        "production_binding": _PRODUCTION_BINDING,
        "reason": "diagnostic_arguments_invalid",
        "required_host_spi": _HOST_SPI_CONTRACT,
        "required_spi_version": HOST_SPI_VERSION,
    }


def _host_binding_error_payload(reason: str) -> dict[str, object]:
    return {
        "action": "verify_running_hermes_agent_installation",
        "binding": "unbound_host_probe",
        "compatible": False,
        "core_patch_map": _CORE_PATCH_MAP,
        "next_step": "verify_running_hermes_agent_installation",
        "probe_scope": _PROBE_SCOPE,
        "production_binding": _PRODUCTION_BINDING,
        "reason": reason,
        "required_host_spi": _HOST_SPI_CONTRACT,
        "required_spi_version": HOST_SPI_VERSION,
    }


def _live_host_report(
    expected_source_root: Path | None,
    expected_host_pid: int | None = None,
    expected_host_executable: Path | None = None,
) -> dict[str, object]:
    hermes_cli = importlib.import_module("hermes_cli")
    plugins = importlib.import_module("hermes_cli.plugins")
    module_source_root, distribution_version = _validate_host_installation(
        hermes_cli,
        expected_source_root,
    )
    context = plugins.PluginContext(
        plugins.PluginManifest(
            name="hermes-agent-plugin-diagnostic",
            source="diagnostic",
            key="hermes-agent-plugin-diagnostic",
        ),
        plugins.PluginManager(),
    )
    payload = diagnose_host_context(context).as_safe_dict()
    payload["binding"] = _ISOLATED_PROBE_BINDING
    payload["probe_scope"] = _PROBE_SCOPE
    payload["production_binding"] = _PRODUCTION_BINDING
    payload["host_distribution"] = "hermes-agent"
    payload["host_distribution_version"] = distribution_version
    payload["host_module"] = "hermes_cli"
    payload["host_source_root"] = str(module_source_root)
    payload["host_version"] = str(getattr(hermes_cli, "__version__", "unknown"))
    if expected_source_root is not None:
        payload["source_matches"] = True
    if expected_host_pid is not None or expected_host_executable is not None:
        if (
            expected_source_root is None
            or expected_host_pid is None
            or expected_host_executable is None
        ):
            raise HostProcessBindingMismatch
        source_root = expected_source_root.resolve(strict=True)
        evidence, launch_kind = _validate_live_process_binding(
            source_root,
            expected_host_pid,
            expected_host_executable,
        )
        payload.update(
            {
                "binding": "verified_live_process_installation",
                "host_process_executable": evidence.executable,
                "host_process_id": expected_host_pid,
                "host_process_launch": launch_kind,
                "host_process_matches": True,
                "host_process_parent_executable": evidence.parent_executable,
                "host_process_parent_id": evidence.parent_process_id,
                "probe_scope": _PROBE_SCOPE,
                "production_binding": _PRODUCTION_BINDING,
            }
        )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = _SafeArgumentParser(
        description="Inspect the public Hermes gateway-extension Host SPI."
    )
    parser.add_argument(
        "--expected-source-root",
        type=_absolute_path,
        help="Confirm that hermes_cli was imported from this source tree.",
    )
    parser.add_argument(
        "--expected-host-pid",
        type=_positive_process_id,
        help="Bind the installation probe to an explicitly selected live Host PID.",
    )
    parser.add_argument(
        "--expected-host-executable",
        type=_absolute_path,
        help="Expected executable path for the explicitly selected live Host PID.",
    )
    try:
        arguments = parser.parse_args(argv)
        _validate_argument_combination(arguments)
    except DiagnosticArgumentError:
        print(json.dumps(_argument_error_payload(), sort_keys=True))
        return _ARGUMENT_ERROR_EXIT_CODE
    try:
        payload = _live_host_report(
            arguments.expected_source_root,
            arguments.expected_host_pid,
            arguments.expected_host_executable,
        )
    except HostProcessBindingMismatch:
        payload = _host_binding_error_payload("host_process_binding_mismatch")
        print(json.dumps(payload, sort_keys=True))
        return 3
    except Exception:
        payload = _host_binding_error_payload("host_probe_failed")
        print(json.dumps(payload, sort_keys=True))
        return 3
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["compatible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HostCompatibilityReport",
    "diagnose_host_context",
    "main",
]
