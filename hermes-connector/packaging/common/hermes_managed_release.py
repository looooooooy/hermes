"""Production entrypoint for Hermes Managed Runtime release assembly.

The legacy ReleaseBuilder remains the deterministic immutable layout engine. This
module is the customer-runtime composition root and makes a verified private
toolchain, a closed lock-bound wheelhouse, and (for portable Plugin manifest v2)
an external cryptographic verifier mandatory.
"""

from __future__ import annotations

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


class ManagedReleaseBuilder(ReleaseBuilder):
    """ReleaseBuilder specialization for customer runtime assembly.

    - `uv sync` installs runtime dependencies only (`--no-default-groups`).
    - portable Plugin manifest v2 is accepted only as an artifact identity; customer
      absolute wheel/store paths are derived locally and are never part of the vendor
      signed payload.
    """

    @staticmethod
    def _commands(inputs: ReleaseInputs, release_dir: Path) -> tuple[BuildCommand, ...]:
        commands = ReleaseBuilder._commands(inputs, release_dir)
        hardened: list[BuildCommand] = []
        for command in commands:
            argv = command.argv
            if len(argv) >= 2 and argv[:2] == ("uv", "sync"):
                if "--no-default-groups" in argv:
                    raise RuntimeError("duplicate --no-default-groups in managed release command")
                argv = (*argv, "--no-default-groups")
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
        if _is_portable_plugin_manifest(inputs.signed_plugin_manifest):
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
