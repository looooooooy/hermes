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
    PublishedRelease,
    ReleaseBuilder,
    ReleaseInputs,
    ReleasePlan,
)
from hermes_offline_wheelhouse import VerifiedWheelhouseV1
from hermes_private_toolchain import PinnedToolchainRunner, PrivateToolchainV1


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
        self._builder = ReleaseBuilder(
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
