"""Production entrypoint for Hermes Managed Runtime release assembly.

The legacy ReleaseBuilder remains the deterministic immutable layout engine. This
module is the customer-runtime composition root and makes a verified private
toolchain, a closed lock-bound wheelhouse, and (for portable Plugin manifest v2)
an external cryptographic verifier mandatory.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
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
project_key = expected_project.lower().replace("-", "_").replace(".", "_")
unexpected_direct_urls = []
for direct_url in (item for root in site_roots for item in root.glob("*.dist-info/direct_url.json")):
    if not direct_url.parent.name.lower().startswith(project_key + "-"):
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
    """ReleaseBuilder specialization for customer runtime assembly.

    - frozen locks are exported to hashed requirements and installed only from the
      verified wheelhouse into a new private-Python virtual environment.
    - venv interpreter/console verification is platform-correct on macOS/Linux/Windows.
    - portable Plugin manifest v2 is accepted only as an artifact identity; customer
      absolute wheel/store paths are derived locally and are never part of the vendor
      signed payload.
    """

    @staticmethod
    def _commands(inputs: ReleaseInputs, release_dir: Path) -> tuple[BuildCommand, ...]:
        commands = ReleaseBuilder._commands(inputs, release_dir)
        hardened: list[BuildCommand] = []
        for command in commands:
            argv = tuple(_managed_venv_python(value) for value in command.argv)
            if len(argv) >= 2 and argv[:2] == ("uv", "sync"):
                component = command.purpose.removeprefix("sync-").removesuffix(
                    "-dependencies"
                )
                if component not in {"host", "connector"}:
                    raise RuntimeError("unexpected managed dependency sync command")
                component_root = release_dir / component
                project = component_root / "project"
                venv = component_root / "venv"
                requirements = component_root / "locked-requirements.txt"
                hardened.extend(
                    (
                        BuildCommand(
                            purpose=f"export-{component}-locked-requirements",
                            argv=(
                                "uv",
                                "export",
                                "--offline",
                                "--frozen",
                                "--project",
                                str(project),
                                "--format",
                                "requirements-txt",
                                "--no-emit-project",
                                "--no-default-groups",
                                "--output-file",
                                str(requirements),
                            ),
                            cwd=project,
                            environment=command.environment,
                            release_dir=command.release_dir,
                        ),
                        BuildCommand(
                            purpose=f"create-{component}-venv",
                            argv=("uv", "venv", "--offline", str(venv)),
                            cwd=component_root,
                            environment=command.environment,
                            release_dir=command.release_dir,
                        ),
                        BuildCommand(
                            purpose=f"install-{component}-locked-dependencies",
                            argv=(
                                "uv",
                                "pip",
                                "install",
                                "--offline",
                                "--python",
                                _managed_venv_python(str(venv / "bin" / "python")),
                                "--require-hashes",
                                "--requirements",
                                str(requirements),
                            ),
                            cwd=component_root,
                            environment=command.environment,
                            release_dir=command.release_dir,
                        ),
                    )
                )
                continue
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
        # The base validator owns local-path and trust-store structural checks. It does
        # not cryptographically verify the signature, so v2 callers must separately
        # provide the mandatory verifier callback enforced by ManagedReleaseAssembler.
        super()._validate_inputs(replace(inputs, signed_plugin_manifest=local_binding))


class ManagedReleaseAssembler:
    """Build one immutable release from only declared Hermes release inputs."""

    def __init__(
        self,
        *,
        releases_root: Path,
        toolchain: PrivateToolchainV1,
        wheelhouse: VerifiedWheelhouseV1,
        service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
        executor: Any | None = None,
        portable_plugin_verifier: Callable[[ReleaseInputs], None] | None = None,
    ) -> None:
        self._wheelhouse = wheelhouse
        self._portable_plugin_verifier = portable_plugin_verifier
        self._runner = PinnedToolchainRunner(
            toolchain,
            wheelhouse=wheelhouse,
            executor=executor,
        )
        self._builder = ManagedReleaseBuilder(
            releases_root=Path(releases_root),
            runner=self._runner,
            service_renderer=service_renderer,
        )

    def build(
        self,
        inputs: ReleaseInputs,
        *,
        dry_run: bool = False,
    ) -> ReleasePlan | PublishedRelease:
        self._wheelhouse.require_lock("core", inputs.core.lock.sha256)
        self._wheelhouse.require_lock("connector", inputs.connector.lock.sha256)
        portable_manifest = getattr(inputs, "signed_plugin_manifest", None)
        if _is_portable_plugin_manifest(portable_manifest):
            if self._portable_plugin_verifier is None:
                raise RuntimeError(
                    "portable Plugin manifest v2 requires external cryptographic verification"
                )
            self._portable_plugin_verifier(inputs)
        return self._builder.build(inputs, dry_run=dry_run)


def build_managed_release(
    *,
    releases_root: Path,
    toolchain: PrivateToolchainV1,
    wheelhouse: VerifiedWheelhouseV1,
    inputs: ReleaseInputs,
    service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
    executor: Any | None = None,
    portable_plugin_verifier: Callable[[ReleaseInputs], None] | None = None,
    dry_run: bool = False,
) -> ReleasePlan | PublishedRelease:
    """Functional composition helper used by installer/update orchestration."""

    return ManagedReleaseAssembler(
        releases_root=releases_root,
        toolchain=toolchain,
        wheelhouse=wheelhouse,
        service_renderer=service_renderer,
        executor=executor,
        portable_plugin_verifier=portable_plugin_verifier,
    ).build(inputs, dry_run=dry_run)


def _managed_venv_python(value: str) -> str:
    if os.name != "nt":
        return value
    path = Path(value)
    if path.name == "python" and path.parent.name == "bin":
        return str(path.parent.parent / "Scripts" / "python.exe")
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
