"""Business API application assembly."""

import os
from collections.abc import Iterable, Mapping
from importlib.util import find_spec
from typing import Any

from hermes_cloud.application.business_api import (
    BusinessApiApplicationPort,
    build_business_api_application,
)
from hermes_cloud.ports.dependency_probe import DependencyProbe


class _UnavailableBusinessApiRuntimeProbe:
    name = "business-api-runtime-configuration"
    critical = True
    deadline_seconds = 1.0

    async def check(self) -> None:
        raise RuntimeError("business API runtime configuration is unavailable")


def build_application(
    dependency_probes: Iterable[DependencyProbe] = (),
    *,
    environment: Mapping[str, str] | None = None,
    **composition: Any,
) -> BusinessApiApplicationPort:
    if composition:
        return build_business_api_application(dependency_probes, **composition)

    values = os.environ if environment is None else environment
    required = {
        "HERMES_RUNTIME_DSN_FILE",
        "HERMES_SIGNING_SECRET_FILE",
    }
    if not required <= set(values) or find_spec("sqlalchemy") is None:
        return build_business_api_application(
            (*dependency_probes, _UnavailableBusinessApiRuntimeProbe())
        )

    from hermes_cloud.adapters.business_api_runtime import (
        build_production_business_api_application,
    )

    return build_production_business_api_application(
        dependency_probes,
        environment=values,
    )
