"""Production entrypoint for Hermes Managed Runtime release assembly.

The legacy ReleaseBuilder remains the deterministic immutable layout engine.  This
module is the customer-runtime composition root and makes both a verified private
toolchain and a closed, lock-bound wheelhouse mandatory.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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


class ManagedReleaseBuilder(ReleaseBuilder):
    """ReleaseBuilder specialization that installs runtime dependencies only.

    `uv sync` includes a project's default dependency groups unless told not to. Both
    Hermes Core and Connector carry developer groups, so customer runtime assembly must
    make `--no-default-groups` part of the audited command/receipt instead of relying on
    a runner-side hidden rewrite.
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
    ) -> None:
        self._wheelhouse = wheelhouse
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
        return self._builder.build(inputs, dry_run=dry_run)


def build_managed_release(
    *,
    releases_root: Path,
    toolchain: PrivateToolchainV1,
    wheelhouse: VerifiedWheelhouseV1,
    inputs: ReleaseInputs,
    service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
    executor: Any | None = None,
    dry_run: bool = False,
) -> ReleasePlan | PublishedRelease:
    """Functional composition helper used by installer/update orchestration."""

    return ManagedReleaseAssembler(
        releases_root=releases_root,
        toolchain=toolchain,
        wheelhouse=wheelhouse,
        service_renderer=service_renderer,
        executor=executor,
    ).build(inputs, dry_run=dry_run)
