"""Production entrypoint for Hermes Managed Runtime release assembly.

The legacy ReleaseBuilder remains the deterministic immutable layout engine. Customer
assembly adds a target-specific, hash-bound runtime install plan derived from the same
Core/Connector uv locks, avoiding universal-lock re-resolution on a single OS while
preserving lock and wheelhouse provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from hermes_local_release import (
    BuildCommand,
    PublishedRelease,
    ReleaseBuilder,
    ReleaseInputs,
    ReleasePlan,
)
from hermes_offline_wheelhouse import VerifiedWheelhouseV1
from hermes_private_toolchain import PinnedToolchainRunner, PrivateToolchainV1
from hermes_target_runtime_plan import VerifiedTargetRuntimePlanV1

_PORTABLE_PLUGIN_FIELDS = {
    "schema_version",
    "plugin_id",
    "version",
    "artifact_filename",
    "wheel_sha256",
    "entrypoint",
    "signature_algorithm",
    "key_id",
    "issued_at",
    "expires_at",
    "signature",
}
_ENTRYPOINT = {
    "group": "hermes_agent.plugins",
    "name": "hermes-agent-plugin",
    "value": "hermes_agent_plugin",
}
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\Z")
_VERIFY_RUNTIME_CROSS_PLATFORM = r"""
import importlib.metadata
import importlib.util
import json
import pathlib
import sys

module_name, console_name, expected_entrypoint, expected_project = sys.argv[1:]
spec = importlib.util.find_spec(module_name)
if spec is None or spec.origin is None:
    raise SystemExit(f"module not found: {module_name}")
matches = [ep for ep in importlib.metadata.entry_points(group="console_scripts") if ep.name == console_name]
if len(matches) != 1 or matches[0].value != expected_entrypoint:
    raise SystemExit(f"console entrypoint mismatch: {console_name}")
console_root = pathlib.Path(sys.executable).parent
console_candidates = [console_root / console_name]
if sys.platform == "win32":
    console_candidates.append(console_root / f"{console_name}.exe")
existing_console = [path for path in console_candidates if path.is_file() and not path.is_symlink()]
if len(existing_console) != 1:
    raise SystemExit(f"console script missing or ambiguous: {console_candidates}")
console_path = existing_console[0]
site_roots = [pathlib.Path(value).resolve() for value in sys.path if "site-packages" in value]
project_key = expected_project.lower().replace("-", "_")
unexpected_direct_urls = []
for direct_url in (item for root in site_roots for item in root.glob("*.dist-info/direct_url.json")):
    if not direct_url.parent.name.lower().replace("-", "_").startswith(project_key + "-"):
        unexpected_direct_urls.append(str(direct_url))
pth_escapes = []
for pth in (item for root in site_roots for item in root.glob("*.pth")):
    for line in pth.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or value.startswith("import "):
            continue
        candidate = (pth.parent / value).resolve() if not pathlib.Path(value).is_absolute() else pathlib.Path(value).resolve()
        if not any(candidate == root or root in candidate.parents for root in site_roots):
            pth_escapes.append(str(candidate))
print(json.dumps({
    "module_origin": str(pathlib.Path(spec.origin).resolve()),
    "console_entrypoint": str(console_path.resolve()),
    "unexpected_direct_urls": unexpected_direct_urls,
    "pth_escapes": pth_escapes,
}, sort_keys=True))
""".strip()


class ManagedReleaseBuilder(ReleaseBuilder):
    """ReleaseBuilder specialization for customer runtime assembly."""

    def __init__(
        self,
        *,
        releases_root: Path,
        runner: Any,
        runtime_plan: VerifiedTargetRuntimePlanV1 | None,
        private_python: Path | None = None,
        service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
    ) -> None:
        super().__init__(
            releases_root=releases_root,
            runner=runner,
            service_renderer=service_renderer,
        )
        self._runtime_plan = runtime_plan
        self._private_python = None if private_python is None else Path(private_python)
        if self._runtime_plan is not None:
            if (
                self._private_python is None
                or not self._private_python.is_absolute()
                or self._private_python.is_symlink()
                or not self._private_python.is_file()
            ):
                raise RuntimeError(
                    "target runtime assembly requires the verified Private Python executable"
                )

    def _prepare_staging(
        self,
        staging: Path,
        inputs: ReleaseInputs,
        services: Mapping[str, bytes],
    ) -> None:
        ReleaseBuilder._prepare_staging(staging, inputs, services)
        if self._runtime_plan is not None:
            _copy_plan_input(
                self._runtime_plan.requirement("core").path,
                self._runtime_plan.requirement("core").sha256,
                staging / "host/project/runtime-requirements.txt",
            )
            _copy_plan_input(
                self._runtime_plan.requirement("connector").path,
                self._runtime_plan.requirement("connector").sha256,
                staging / "connector/project/runtime-requirements.txt",
            )
            _copy_plan_input(
                self._runtime_plan.plan_path,
                self._runtime_plan.plan_sha256,
                staging / "receipts/runtime-install-plan.json",
            )

        # chmod(0400) maps to the Windows read-only attribute. A failed command would
        # otherwise make shutil.rmtree mask the real resolver error with AccessDenied.
        # Keep staging files writable until ReleaseBuilder's final immutable freeze.
        if os.name == "nt":
            for path in (staging / "plugin").rglob("*"):
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o600)

    def _commands(self, inputs: ReleaseInputs, release_dir: Path) -> tuple[BuildCommand, ...]:
        if self._runtime_plan is None:
            return _harden_legacy_commands(ReleaseBuilder._commands(inputs, release_dir))
        if self._private_python is None:
            raise RuntimeError("target runtime Private Python binding is missing")
        return _target_runtime_commands(inputs, release_dir, self._private_python)

    def _execute(
        self,
        commands: tuple[BuildCommand, ...],
        staging: Path,
        release_dir: Path,
    ) -> dict[str, Mapping[str, Any]]:
        verification: dict[str, Mapping[str, Any]] = {}
        for command in commands:
            result = self._runner.run(command)
            if command.purpose.startswith("verify-"):
                runtime = "host" if command.purpose == "verify-host-runtime" else "connector"
                verification[runtime] = _validate_managed_verification(
                    result.stdout,
                    staging / runtime / "venv",
                    release_dir / runtime / "venv",
                    command.argv[-3],
                )
        if set(verification) != {"host", "connector"}:
            raise RuntimeError("managed runtime verification receipts are incomplete")
        return verification

    def _validate_inputs(self, inputs: ReleaseInputs) -> None:
        manifest = inputs.signed_plugin_manifest
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 2:
            super()._validate_inputs(inputs)
            return

        _validate_portable_plugin_manifest(inputs)
        release_dir = self._root / inputs.release_id
        expected_wheel = (
            release_dir
            / "plugin"
            / "artifacts"
            / "hermes-agent-plugin"
            / str(manifest["version"])
            / inputs.plugin_bundle.sha256
            / inputs.plugin_bundle.path.name
        ).resolve(strict=False)
        local_store_root = (self._root.parent / "plugin-state").resolve(strict=False)
        local_binding = {
            "schema_version": 1,
            "plugin_id": manifest["plugin_id"],
            "version": manifest["version"],
            "wheel_path": str(expected_wheel),
            "wheel_sha256": manifest["wheel_sha256"],
            "store_root": str(local_store_root),
            "entrypoint": dict(manifest["entrypoint"]),
            "signature_algorithm": manifest["signature_algorithm"],
            "key_id": manifest["key_id"],
            "issued_at": manifest["issued_at"],
            "expires_at": manifest["expires_at"],
            "signature": manifest["signature"],
        }
        super()._validate_inputs(replace(inputs, signed_plugin_manifest=local_binding))


class ManagedReleaseAssembler:
    """Build one immutable release from only declared Hermes release inputs."""

    def __init__(
        self,
        *,
        releases_root: Path,
        toolchain: PrivateToolchainV1,
        wheelhouse: VerifiedWheelhouseV1,
        runtime_plan: VerifiedTargetRuntimePlanV1 | None = None,
        service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
        executor: Any | None = None,
        portable_plugin_verifier: Callable[[ReleaseInputs], None] | None = None,
    ) -> None:
        self._wheelhouse = wheelhouse
        self._runtime_plan = runtime_plan
        self._portable_plugin_verifier = portable_plugin_verifier
        self._runner = PinnedToolchainRunner(
            toolchain,
            wheelhouse=wheelhouse,
            executor=executor,
        )
        builder_args: dict[str, Any] = {
            "releases_root": Path(releases_root),
            "runner": self._runner,
            "runtime_plan": runtime_plan,
            "service_renderer": service_renderer,
        }
        if runtime_plan is not None:
            builder_args["private_python"] = toolchain.python.path
        self._builder = ManagedReleaseBuilder(**builder_args)

    def build(
        self,
        inputs: ReleaseInputs,
        *,
        dry_run: bool = False,
    ) -> ReleasePlan | PublishedRelease:
        self._wheelhouse.require_lock("core", inputs.core.lock.sha256)
        self._wheelhouse.require_lock("connector", inputs.connector.lock.sha256)
        if self._runtime_plan is not None:
            self._runtime_plan.require_lock("core", inputs.core.lock.sha256)
            self._runtime_plan.require_lock("connector", inputs.connector.lock.sha256)
            if self._runtime_plan.wheelhouse_manifest_sha256 != self._wheelhouse.manifest_sha256:
                raise RuntimeError("target runtime plan is not bound to verified wheelhouse")
            if (
                self._runtime_plan.platform != self._wheelhouse.platform
                or self._runtime_plan.architecture != self._wheelhouse.architecture
                or self._runtime_plan.python_tag != self._wheelhouse.python_tag
            ):
                raise RuntimeError("target runtime plan target does not match wheelhouse")
        portable_manifest = getattr(inputs, "signed_plugin_manifest", None)
        if _is_portable_plugin_manifest(portable_manifest):
            if self._portable_plugin_verifier is None:
                raise RuntimeError(
                    "portable Plugin manifest v2 requires external cryptographic verification"
                )
            self._portable_plugin_verifier(inputs)
        published = self._builder.build(inputs, dry_run=dry_run)
        if not dry_run and self._runtime_plan is not None:
            _verify_runtime_plan_receipt(published.release_dir, self._runtime_plan)
        return published


def build_managed_release(
    *,
    releases_root: Path,
    toolchain: PrivateToolchainV1,
    wheelhouse: VerifiedWheelhouseV1,
    inputs: ReleaseInputs,
    runtime_plan: VerifiedTargetRuntimePlanV1 | None = None,
    service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
    executor: Any | None = None,
    portable_plugin_verifier: Callable[[ReleaseInputs], None] | None = None,
    dry_run: bool = False,
) -> ReleasePlan | PublishedRelease:
    return ManagedReleaseAssembler(
        releases_root=releases_root,
        toolchain=toolchain,
        wheelhouse=wheelhouse,
        runtime_plan=runtime_plan,
        service_renderer=service_renderer,
        executor=executor,
        portable_plugin_verifier=portable_plugin_verifier,
    ).build(inputs, dry_run=dry_run)


def _target_runtime_commands(
    inputs: ReleaseInputs,
    release_dir: Path,
    private_python: Path,
) -> tuple[BuildCommand, ...]:
    host_project = release_dir / "host/project"
    connector_project = release_dir / "connector/project"
    host_venv = release_dir / "host/venv"
    connector_venv = release_dir / "connector/venv"
    host_python = _venv_python(host_venv)
    connector_python = _venv_python(connector_venv)
    host_wheel = release_dir / "receipts/inputs/core" / inputs.core.wheel.path.name
    connector_wheel = (
        release_dir / "receipts/inputs/connector" / inputs.connector.wheel.path.name
    )

    def command(purpose: str, argv: tuple[str, ...], cwd: Path) -> BuildCommand:
        return BuildCommand(
            purpose=purpose,
            argv=argv,
            cwd=cwd,
            environment=MappingProxyType({"UV_OFFLINE": "1"}),
            release_dir=release_dir,
        )

    create_venv = lambda destination: (
        str(private_python),
        "-I",
        "-m",
        "venv",
        "--without-pip",
        "--copies",
        str(destination),
    )
    return (
        command("create-host-venv", create_venv(host_venv), release_dir),
        command(
            "install-host-dependencies",
            (
                "uv", "pip", "install", "--offline", "--python", str(host_python),
                "--require-hashes", "--no-deps", "--requirement",
                str(host_project / "runtime-requirements.txt"),
            ),
            host_project,
        ),
        command(
            "install-final-core-wheel",
            (
                "uv", "pip", "install", "--offline", "--python", str(host_python),
                "--no-deps", str(host_wheel),
            ),
            release_dir,
        ),
        command(
            "verify-host-runtime",
            (
                str(host_python), "-I", "-c", _VERIFY_RUNTIME_CROSS_PLATFORM,
                inputs.core.launch_module, inputs.core.console_script,
                inputs.core.entrypoint, inputs.core.project_name,
            ),
            release_dir,
        ),
        command("create-connector-venv", create_venv(connector_venv), release_dir),
        command(
            "install-connector-dependencies",
            (
                "uv", "pip", "install", "--offline", "--python", str(connector_python),
                "--require-hashes", "--no-deps", "--requirement",
                str(connector_project / "runtime-requirements.txt"),
            ),
            connector_project,
        ),
        command(
            "install-final-connector-wheel",
            (
                "uv", "pip", "install", "--offline", "--python", str(connector_python),
                "--no-deps", str(connector_wheel),
            ),
            release_dir,
        ),
        command(
            "verify-connector-runtime",
            (
                str(connector_python), "-I", "-c", _VERIFY_RUNTIME_CROSS_PLATFORM,
                inputs.connector.launch_module, inputs.connector.console_script,
                inputs.connector.entrypoint, inputs.connector.project_name,
            ),
            release_dir,
        ),
    )


def _harden_legacy_commands(commands: tuple[BuildCommand, ...]) -> tuple[BuildCommand, ...]:
    hardened: list[BuildCommand] = []
    for command in commands:
        argv = tuple(_managed_venv_python(value) for value in command.argv)
        if len(argv) >= 2 and argv[:2] == ("uv", "sync"):
            if "--no-default-groups" in argv:
                raise RuntimeError("duplicate --no-default-groups in managed release command")
            argv = (*argv, "--no-default-groups")
        if command.purpose.startswith("verify-"):
            values = list(argv)
            try:
                code_index = values.index("-c") + 1
            except (ValueError, IndexError) as error:
                raise RuntimeError("managed runtime verification command is malformed") from error
            values[code_index] = _VERIFY_RUNTIME_CROSS_PLATFORM
            argv = tuple(values)
        hardened.append(
            BuildCommand(
                purpose=command.purpose,
                argv=argv,
                cwd=command.cwd,
                environment=command.environment,
                release_dir=command.release_dir,
            )
        )
    return tuple(hardened)


def _copy_plan_input(source: Path, expected_sha: str, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("target runtime plan input is missing or symlinked")
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise RuntimeError("target runtime plan input digest changed")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with destination.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        destination.chmod(0o600)


def _verify_runtime_plan_receipt(
    release_dir: Path,
    runtime_plan: VerifiedTargetRuntimePlanV1,
) -> None:
    expected = {
        release_dir / "host/project/runtime-requirements.txt": runtime_plan.requirement("core").sha256,
        release_dir / "connector/project/runtime-requirements.txt": runtime_plan.requirement("connector").sha256,
        release_dir / "receipts/runtime-install-plan.json": runtime_plan.plan_sha256,
    }
    for path, digest in expected.items():
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != digest:
            raise RuntimeError(f"managed runtime install-plan receipt mismatch: {path.name}")


def _validate_managed_verification(
    stdout: str,
    staging_venv: Path,
    final_venv: Path,
    console_script: str,
) -> Mapping[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("managed runtime verification did not return valid JSON") from exc
    if not isinstance(value, dict) or value.get("unexpected_direct_urls") or value.get("pth_escapes"):
        raise RuntimeError("managed runtime verification detected an unsafe installation")
    module_origin = _path_inside(value.get("module_origin"), staging_venv)
    console = _path_inside(value.get("console_entrypoint"), staging_venv)
    candidates = [_venv_console(staging_venv, console_script)]
    if os.name == "nt":
        candidates.append(_venv_console(staging_venv, f"{console_script}.exe"))
    resolved_candidates = {candidate.resolve(strict=False) for candidate in candidates}
    if console not in resolved_candidates:
        raise RuntimeError("managed console entrypoint is not the exact isolated venv executable")
    return {
        "module_origin": str(
            final_venv / module_origin.relative_to(staging_venv.resolve(strict=False))
        ),
        "console_entrypoint": str(
            final_venv / console.relative_to(staging_venv.resolve(strict=False))
        ),
        "unexpected_direct_urls": [],
        "pth_escapes": [],
    }


def _path_inside(raw: object, root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("managed runtime verification path is missing")
    path = Path(raw).resolve(strict=False)
    expected = root.resolve(strict=False)
    try:
        path.relative_to(expected)
    except ValueError as exc:
        raise RuntimeError("managed runtime verification path escaped isolated venv") from exc
    return path


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts/python.exe"
    return venv / "bin/python"


def _venv_console(venv: Path, name: str) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / name
    return venv / "bin" / name


def _managed_venv_python(value: str) -> str:
    if os.name != "nt":
        return value
    path = Path(value)
    if path.name == "python" and path.parent.name == "bin":
        return str(path.parent.parent / "Scripts/python.exe")
    return value


def _is_portable_plugin_manifest(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("schema_version") == 2


def _validate_portable_plugin_manifest(inputs: ReleaseInputs) -> None:
    manifest = inputs.signed_plugin_manifest
    if not isinstance(manifest, Mapping) or set(manifest) != _PORTABLE_PLUGIN_FIELDS:
        raise RuntimeError("portable Plugin manifest does not match schema v2")
    if manifest["schema_version"] != 2 or manifest["plugin_id"] != "hermes-agent-plugin":
        raise RuntimeError("portable Plugin identity is invalid")
    version = manifest["version"]
    if not isinstance(version, str) or not version or version != version.strip() or len(version) > 64:
        raise RuntimeError("portable Plugin version is invalid")
    filename = manifest["artifact_filename"]
    if (
        not isinstance(filename, str)
        or _SAFE_FILENAME.fullmatch(filename) is None
        or not filename.endswith(".whl")
        or filename != inputs.plugin_bundle.path.name
    ):
        raise RuntimeError("portable Plugin artifact filename is invalid")
    if manifest["wheel_sha256"] != inputs.plugin_bundle.sha256:
        raise RuntimeError("portable Plugin wheel digest does not match artifact")
    if dict(manifest["entrypoint"]) != _ENTRYPOINT:
        raise RuntimeError("portable Plugin entrypoint is invalid")
    if manifest["signature_algorithm"] != "ed25519":
        raise RuntimeError("portable Plugin signature algorithm is invalid")
    for field in ("key_id", "issued_at", "expires_at", "signature"):
        value = manifest[field]
        if not isinstance(value, str) or not value or value != value.strip():
            raise RuntimeError(f"portable Plugin {field} is invalid")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
