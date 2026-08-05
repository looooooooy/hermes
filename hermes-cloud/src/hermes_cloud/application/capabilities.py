"""Platform-neutral v1 capability validation and negotiation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from hermes_cloud.domain.contract_errors import CoreContractError

CAPABILITY_CATALOG = (
    "session.observe",
    "session.observe.output-parity.v1",
    "session.control",
    "session.catalog.v1",
    "file.exchange",
    "a2a.message",
    "view.card",
    "view.interaction",
    "enterprise.data",
    "mcp.app",
)
_CAPABILITY_SET = frozenset(CAPABILITY_CATALOG)
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]+$")


class CapabilityNegotiationError(CoreContractError):
    """Capability input failed v1 validation or required resolution."""


@dataclass(frozen=True)
class CapabilityManifest:
    contract_version: int
    runtime_generation: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityWelcome:
    runtime_generation: str
    profile: str
    accepted_capabilities: tuple[str, ...]
    unavailable_optional_capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": 1,
            "message_type": "local.welcome",
            "runtime_generation": self.runtime_generation,
            "profile": self.profile,
            "accepted_capabilities": list(self.accepted_capabilities),
            "unavailable_optional_capabilities": list(
                self.unavailable_optional_capabilities
            ),
        }


def _raise(category: str) -> None:
    raise CapabilityNegotiationError(category)


def _validate_text(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        _raise("invalid_envelope")
    if "\x00" in value or any("\ud800" <= character <= "\udfff" for character in value):
        _raise("invalid_envelope")
    return value


def _validate_capabilities(
    values: object,
    *,
    require_catalog: bool,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        _raise("invalid_envelope")
    if len(values) > 64:
        _raise("invalid_envelope")
    validated = tuple(_validate_text(value) for value in values)
    if len(validated) != len(set(validated)):
        _raise("invalid_envelope")
    if require_catalog and not set(validated) <= _CAPABILITY_SET:
        _raise("invalid_envelope")
    return validated


def validate_capability_manifest(
    value: object,
) -> CapabilityManifest:
    if not isinstance(value, dict):
        _raise("invalid_envelope")
    if set(value) != {
        "contract_version",
        "runtime_generation",
        "capabilities",
    }:
        _raise("invalid_envelope")
    version = value["contract_version"]
    if type(version) is not int or version != 1:
        _raise("contract_unsupported")
    generation = _validate_text(value["runtime_generation"])
    capabilities = _validate_capabilities(
        value["capabilities"],
        require_catalog=True,
    )
    return CapabilityManifest(version, generation, capabilities)


def negotiate_capabilities(
    *,
    required_capabilities: Sequence[str],
    optional_capabilities: Sequence[str],
    available_capabilities: Sequence[str],
    runtime_generation: str,
    profile: str,
) -> CapabilityWelcome:
    required = _validate_capabilities(
        required_capabilities,
        require_catalog=False,
    )
    optional = _validate_capabilities(
        optional_capabilities,
        require_catalog=False,
    )
    available = frozenset(
        _validate_capabilities(
            available_capabilities,
            require_catalog=True,
        )
    )
    generation = _validate_text(runtime_generation)
    validated_profile = _validate_text(profile)
    if not _PROFILE.fullmatch(validated_profile):
        _raise("invalid_envelope")

    if any(capability not in available for capability in required):
        _raise("capability_not_available")

    accepted: list[str] = []
    for capability in (*required, *optional):
        if capability in available and capability not in accepted:
            accepted.append(capability)
    unavailable_optional = tuple(
        capability for capability in optional if capability not in available
    )
    return CapabilityWelcome(
        runtime_generation=generation,
        profile=validated_profile,
        accepted_capabilities=tuple(accepted),
        unavailable_optional_capabilities=unavailable_optional,
    )
