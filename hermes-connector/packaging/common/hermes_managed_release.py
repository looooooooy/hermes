"""Production entrypoint for Hermes Managed Runtime release assembly.

The legacy ReleaseBuilder remains the deterministic immutable layout engine.  This
module is the customer-runtime composition root and deliberately makes a verified
PrivateToolchainV1 mandatory, so production assembly cannot silently discover uv or
Python from the host PATH.
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
from hermes_private_toolchain import PinnedToolchainRunner, PrivateToolchainV1


class ManagedReleaseAssembler:
    """Build one immutable release with an explicitly pinned Hermes toolchain."""

    def __init__(
        self,
        *,
        releases_root: Path,
        toolchain: PrivateToolchainV1,
        service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
        executor: Any | None = None,
    ) -> None:
        self._runner = PinnedToolchainRunner(toolchain, executor=executor)
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
        return self._builder.build(inputs, dry_run=dry_run)


def build_managed_release(
    *,
    releases_root: Path,
    toolchain: PrivateToolchainV1,
    inputs: ReleaseInputs,
    service_renderer: Callable[[Path], Mapping[str, bytes]] | None = None,
    executor: Any | None = None,
    dry_run: bool = False,
) -> ReleasePlan | PublishedRelease:
    """Functional composition helper used by installer/update orchestration."""

    return ManagedReleaseAssembler(
        releases_root=releases_root,
        toolchain=toolchain,
        service_renderer=service_renderer,
        executor=executor,
    ).build(inputs, dry_run=dry_run)
