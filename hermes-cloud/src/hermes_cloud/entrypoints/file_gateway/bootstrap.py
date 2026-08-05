"""File Gateway process bootstrap."""

from collections.abc import Iterable

from hermes_cloud.application.asgi_health import HealthApplication
from hermes_cloud.ports.dependency_probe import DependencyProbe

from .app import build_application


def create_app(
    dependency_probes: Iterable[DependencyProbe] = (),
) -> HealthApplication:
    return build_application(dependency_probes)


app = create_app()
