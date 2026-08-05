"""File Gateway application assembly."""

from collections.abc import Iterable

from hermes_cloud.application.asgi_health import HealthApplication
from hermes_cloud.ports.dependency_probe import DependencyProbe


def build_application(
    dependency_probes: Iterable[DependencyProbe] = (),
) -> HealthApplication:
    return HealthApplication("file-gateway", dependency_probes)
