"""Async Worker process bootstrap."""

from collections.abc import Iterable

from hermes_cloud.application.worker import WorkerRunner
from hermes_cloud.ports.dependency_probe import DependencyProbe

from .app import build_worker


def create_worker(
    dependency_probes: Iterable[DependencyProbe] = (),
) -> WorkerRunner:
    return build_worker(dependency_probes)


worker = create_worker()
