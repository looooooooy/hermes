"""Async Worker application assembly."""

from collections.abc import Iterable

from hermes_cloud.application.worker import WorkerRunner
from hermes_cloud.ports.dependency_probe import DependencyProbe


def build_worker(
    dependency_probes: Iterable[DependencyProbe] = (),
) -> WorkerRunner:
    return WorkerRunner(dependency_probes)
