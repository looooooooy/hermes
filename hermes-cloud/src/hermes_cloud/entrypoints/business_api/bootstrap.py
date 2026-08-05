"""Business API process bootstrap."""

from collections.abc import Iterable, Mapping
from typing import Any

from hermes_cloud.application.business_api import BusinessApiApplicationPort
from hermes_cloud.ports.dependency_probe import DependencyProbe

from .app import build_application


def create_app(
    dependency_probes: Iterable[DependencyProbe] = (),
    *,
    environment: Mapping[str, str] | None = None,
    **composition: Any,
) -> BusinessApiApplicationPort:
    return build_application(
        dependency_probes,
        environment=environment,
        **composition,
    )


app = create_app()
