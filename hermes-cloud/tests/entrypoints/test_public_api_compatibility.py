from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any

import pytest

EXPECTED_PUBLIC_API = {
    "business_api": {"app", "create_app"},
    "connector_gateway": {
        "ConnectorGatewayApplication",
        "app",
        "create_app",
        "decode_connector_frame",
    },
    "worker": {"create_worker", "worker"},
    "file_gateway": {"app", "create_app"},
}


@pytest.mark.parametrize(
    ("module_name", "factory_name", "singleton_name", "component"),
    [
        ("business_api", "create_app", "app", "business-api"),
        (
            "connector_gateway",
            "create_app",
            "app",
            "connector-gateway",
        ),
        ("file_gateway", "create_app", "app", "file-gateway"),
        ("worker", "create_worker", "worker", "async-worker"),
    ],
)
def test_public_factory_and_singleton_api_remains_importable(
    module_name: str,
    factory_name: str,
    singleton_name: str,
    component: str,
) -> None:
    module = importlib.import_module(f"hermes_cloud.entrypoints.{module_name}")
    factory = getattr(module, factory_name)
    singleton = getattr(module, singleton_name)

    assert callable(factory)
    assert singleton.snapshot()["component"] == component
    assert factory().snapshot()["component"] == component


def test_connector_gateway_public_decoder_and_application_remain_importable() -> None:
    module = importlib.import_module("hermes_cloud.entrypoints.connector_gateway")

    assert callable(module.decode_connector_frame)
    assert module.create_app().__class__ is module.ConnectorGatewayApplication


@pytest.mark.parametrize(
    ("module_name", "factory_name"),
    [
        ("business_api", "create_app"),
        ("connector_gateway", "create_app"),
        ("file_gateway", "create_app"),
    ],
)
def test_http_factory_keeps_dependency_probe_keyword(
    module_name: str,
    factory_name: str,
) -> None:
    module = importlib.import_module(f"hermes_cloud.entrypoints.{module_name}")
    factory: Callable[..., Any] = getattr(module, factory_name)

    assert factory(dependency_probes=[]).snapshot()["state"] == "CREATED"


def test_worker_factory_keeps_dependency_probe_keyword() -> None:
    module = importlib.import_module("hermes_cloud.entrypoints.worker")
    worker = module.create_worker(dependency_probes=[])

    async def scenario() -> None:
        await worker.start()
        assert worker.snapshot()["state"] == "READY"
        await worker.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("module_name", "expected"),
    EXPECTED_PUBLIC_API.items(),
)
def test_package_public_surface_exactly_matches_all(
    module_name: str,
    expected: set[str],
) -> None:
    module = importlib.import_module(f"hermes_cloud.entrypoints.{module_name}")

    assert set(module.__all__) == expected
    assert all(hasattr(module, name) for name in module.__all__)
    assert {name for name in vars(module) if not name.startswith("_")} == expected
