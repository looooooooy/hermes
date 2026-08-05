from __future__ import annotations

from collections.abc import Iterable

from hermes_connector.domain.contract_messages import LocalHello, LocalWelcome


class RequiredCapabilityUnavailable(RuntimeError):
    code = 4304
    error_name = "capability_not_available"

    def __init__(self, missing_capabilities: tuple[str, ...]) -> None:
        super().__init__("required capability is unavailable")
        self.missing_capabilities = missing_capabilities


def negotiate_local_capabilities(
    hello: LocalHello,
    *,
    runtime_generation: str,
    available_capabilities: Iterable[str],
) -> LocalWelcome:
    if (
        not isinstance(runtime_generation, str)
        or not 1 <= len(runtime_generation) <= 128
    ):
        raise ValueError("runtime_generation length is outside contract limits")

    available = frozenset(available_capabilities)
    missing_required = tuple(
        capability
        for capability in hello.required_capabilities
        if capability not in available
    )
    if missing_required:
        raise RequiredCapabilityUnavailable(missing_required)

    accepted = tuple(
        capability
        for capability in (
            *hello.required_capabilities,
            *hello.optional_capabilities,
        )
        if capability in available
    )
    unavailable_optional = tuple(
        capability
        for capability in hello.optional_capabilities
        if capability not in available
    )
    return LocalWelcome(
        contract_version=1,
        message_type="local.welcome",
        runtime_generation=runtime_generation,
        profile=hello.profile,
        accepted_capabilities=accepted,
        unavailable_optional_capabilities=unavailable_optional,
    )
