"""Dependency-safe composition boundary for the public Business API entrypoint."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from importlib.util import find_spec
from typing import Any, Protocol

from hermes_cloud.application.asgi_health import HealthApplication
from hermes_cloud.ports.dependency_probe import DependencyProbe


class BusinessApiApplicationPort(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None: ...


def build_business_api_application(
    dependency_probes: Iterable[DependencyProbe] = (),
    **composition: Any,
) -> BusinessApiApplicationPort:
    if _adapter_dependencies_available():
        from hermes_cloud.modules.cloud_api.adapters.fastapi import (
            build_fastapi_application,
        )

        return build_fastapi_application(dependency_probes, **composition)
    if composition:
        raise RuntimeError("Business API adapter dependencies are unavailable")
    return HealthApplication("business-api", dependency_probes)


def _adapter_dependencies_available() -> bool:
    return all(
        find_spec(module_name) is not None
        for module_name in ("argon2", "fastapi", "jwt")
    )
