from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

CONTRACTS_ROOT = Path(__file__).parent / "fixtures/repository_contracts"


def _load_fixture(relative_path: str) -> dict[str, Any]:
    return json.loads(
        (CONTRACTS_ROOT / "fixtures" / relative_path).read_text(encoding="utf-8")
    )


def _capabilities_module() -> Any:
    return importlib.import_module("hermes_cloud.application.capabilities")


def test_valid_capability_fixture_is_accepted_without_consumer_fields() -> None:
    capabilities = _capabilities_module()

    manifest = capabilities.validate_capability_manifest(
        _load_fixture("valid/capability-manifest.json")
    )

    assert manifest.contract_version == 1
    assert manifest.runtime_generation == "runtime-20260730-01"
    assert manifest.capabilities == (
        "session.observe",
        "session.observe.output-parity.v1",
        "session.control",
        "view.card",
    )
    assert not {"android", "web", "ios", "desktop"} & vars(manifest).keys()


def test_invalid_capability_fixture_is_rejected() -> None:
    capabilities = _capabilities_module()
    error_type = capabilities.CapabilityNegotiationError

    with pytest.raises(error_type) as caught:
        capabilities.validate_capability_manifest(
            _load_fixture("invalid/capability-unknown.json")
        )

    assert caught.value.category == "invalid_envelope"


def test_negotiation_output_matches_authoritative_local_welcome_semantics() -> None:
    capabilities = _capabilities_module()
    hello = _load_fixture("valid/local-gateway-handshake.json")
    expected = _load_fixture("valid/local-gateway-welcome.json")

    welcome = capabilities.negotiate_capabilities(
        required_capabilities=hello["required_capabilities"],
        optional_capabilities=hello["optional_capabilities"],
        available_capabilities=["session.observe"],
        runtime_generation=expected["runtime_generation"],
        profile=hello["profile"],
    )

    assert welcome.as_dict() == expected
    assert not {"android", "web", "ios", "desktop"} & welcome.as_dict().keys()


def test_missing_required_capability_rejects_before_effect() -> None:
    capabilities = _capabilities_module()

    with pytest.raises(capabilities.CapabilityNegotiationError) as caught:
        capabilities.negotiate_capabilities(
            required_capabilities=["enterprise.data"],
            optional_capabilities=[],
            available_capabilities=["session.observe"],
            runtime_generation="runtime-test",
            profile="default",
        )

    assert caught.value.category == "capability_not_available"
    assert caught.value.code == 4304
    assert caught.value.retryable is False


def test_missing_optional_capability_degrades_without_rejection() -> None:
    capabilities = _capabilities_module()

    welcome = capabilities.negotiate_capabilities(
        required_capabilities=["session.observe"],
        optional_capabilities=["view.card", "file.exchange"],
        available_capabilities=["session.observe", "view.card"],
        runtime_generation="runtime-test",
        profile="default",
    )

    assert welcome.accepted_capabilities == (
        "session.observe",
        "view.card",
    )
    assert welcome.unavailable_optional_capabilities == ("file.exchange",)


def test_future_requested_capability_uses_required_optional_semantics() -> None:
    capabilities = _capabilities_module()

    with pytest.raises(capabilities.CapabilityNegotiationError) as caught:
        capabilities.negotiate_capabilities(
            required_capabilities=["future.capability"],
            optional_capabilities=[],
            available_capabilities=["session.observe"],
            runtime_generation="runtime-test",
            profile="default",
        )
    assert caught.value.category == "capability_not_available"

    welcome = capabilities.negotiate_capabilities(
        required_capabilities=["session.observe"],
        optional_capabilities=["future.capability"],
        available_capabilities=["session.observe"],
        runtime_generation="runtime-test",
        profile="default",
    )
    assert welcome.unavailable_optional_capabilities == ("future.capability",)


def test_capability_catalog_matches_authoritative_schema() -> None:
    capabilities = _capabilities_module()
    schema = json.loads(
        (CONTRACTS_ROOT / "schemas/capability-manifest-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    authoritative = schema["properties"]["capabilities"]["items"]["enum"]

    assert list(capabilities.CAPABILITY_CATALOG) == authoritative
