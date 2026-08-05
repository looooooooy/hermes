"""Side-effect-free validation for the public Hermes Host SPI boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, cast

HOST_SPI_VERSION = 1
REQUIRED_HOST_CAPABILITIES = frozenset(
    {
        "audit.safe.v1",
        "extension.lifecycle.v1",
        "runtime.descriptor.v1",
        "session.observe.v1",
        "session.owner-actions.v1",
    }
)
_CONTEXT_MEMBERS = (
    "gateway_extension_capabilities",
    "gateway_extension_spi_version",
    "register_gateway_extension",
)


@dataclass(frozen=True)
class HostSpiValidation:
    """Normalized compatibility decision without runtime composition."""

    compatible: bool
    reason: str
    observed_spi_version: int | None
    missing_context_members: tuple[str, ...] = ()
    missing_required_capabilities: tuple[str, ...] = ()
    register_extension: Callable[..., Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def validate_host_context(context: Any) -> HostSpiValidation:
    """Validate exact Host SPI v1 shapes without importing the Plugin runtime."""
    values: dict[str, object] = {}
    missing: list[str] = []
    for name in _CONTEXT_MEMBERS:
        try:
            values[name] = getattr(context, name)
        except Exception:
            missing.append(name)

    observed_version = values.get("gateway_extension_spi_version")
    safe_version = observed_version if type(observed_version) is int else None
    if "gateway_extension_spi_version" in values and safe_version is None:
        missing.append("gateway_extension_spi_version")

    register_extension = values.get("register_gateway_extension")
    if "register_gateway_extension" in values and not callable(register_extension):
        missing.append("register_gateway_extension")

    capabilities: frozenset[str] | None = None
    if "gateway_extension_capabilities" in values:
        raw_capabilities = values["gateway_extension_capabilities"]
        if type(raw_capabilities) is not frozenset or not all(
            type(capability) is str for capability in raw_capabilities
        ):
            missing.append("gateway_extension_capabilities")
        else:
            capabilities = cast(frozenset[str], raw_capabilities)

    if missing:
        return HostSpiValidation(
            compatible=False,
            reason="missing_context_members",
            observed_spi_version=safe_version,
            missing_context_members=tuple(sorted(set(missing))),
        )
    if safe_version != HOST_SPI_VERSION:
        return HostSpiValidation(
            compatible=False,
            reason="spi_version_mismatch",
            observed_spi_version=safe_version,
        )

    missing_capabilities = REQUIRED_HOST_CAPABILITIES - (capabilities or frozenset())
    if missing_capabilities:
        return HostSpiValidation(
            compatible=False,
            reason="missing_required_capabilities",
            observed_spi_version=safe_version,
            missing_required_capabilities=tuple(sorted(missing_capabilities)),
        )
    return HostSpiValidation(
        compatible=True,
        reason="compatible",
        observed_spi_version=safe_version,
        register_extension=cast(Callable[..., Any], register_extension),
    )


__all__ = [
    "HOST_SPI_VERSION",
    "REQUIRED_HOST_CAPABILITIES",
    "HostSpiValidation",
    "validate_host_context",
]
