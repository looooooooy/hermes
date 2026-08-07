"""Managed-runtime decoration for the public Hermes Host extension."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ...application.update_safety import HostUpdateSafetyAggregator
from .extension import HermesAgentPluginExtension
from .spi_v1 import CompositeRegistration, GatewayExtensionHostV1, HostSpiFactories


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        if name not in value:
            raise RuntimeError("host runtime descriptor is incomplete")
        return value[name]
    try:
        return getattr(value, name)
    except AttributeError as error:
        raise RuntimeError("host runtime descriptor is incomplete") from error


class _HostRuntimeDescriptorBinding:
    """Read the public runtime descriptor without sharing Extension internals."""

    def __init__(self, host: GatewayExtensionHostV1) -> None:
        self._host = host

    def snapshot(self) -> tuple[str, str, bool]:
        descriptor = self._host.runtime_descriptor()
        profile = _field(descriptor, "profile")
        runtime_generation = _field(descriptor, "runtime_generation")
        state = _field(descriptor, "state")
        if not isinstance(profile, str) or not isinstance(runtime_generation, str):
            raise RuntimeError("host runtime descriptor identity is invalid")
        if state not in {"ready", "unavailable"}:
            raise RuntimeError("host runtime descriptor state is invalid")
        return profile, runtime_generation, state == "ready"


class ManagedRuntimeHermesAgentPluginExtension(HermesAgentPluginExtension):
    """Add a private aggregate-only update-safety relay to the base extension."""

    def __init__(
        self,
        *,
        host_spi_factories: HostSpiFactories,
        endpoint_opener: Callable[[object, object], object] | None = None,
        update_safety_opener: Callable[[Callable[[], object]], object] | None = None,
    ) -> None:
        super().__init__(
            host_spi_factories=host_spi_factories,
            endpoint_opener=endpoint_opener,
        )
        self._managed_host_spi_factories = host_spi_factories
        self._update_safety_opener = update_safety_opener

    def install(self, host: GatewayExtensionHostV1) -> object:
        primary = super().install(host)
        opener = self._update_safety_opener
        catalog_request = self._managed_host_spi_factories.session_catalog_request
        if (
            opener is None
            or catalog_request is None
            or not callable(getattr(host, "session_catalog", None))
            or not callable(getattr(host, "control_snapshot", None))
        ):
            return primary

        aggregator = HostUpdateSafetyAggregator(
            host=host,
            binding=_HostRuntimeDescriptorBinding(host),
            session_catalog_request=catalog_request,
            control_scope=self._managed_host_spi_factories.control_scope,
        )
        try:
            relay = opener(aggregator.snapshot)
            if not callable(getattr(relay, "close", None)):
                raise TypeError("update-safety opener must return a registration")
        except BaseException:
            primary.close()
            raise
        return CompositeRegistration([primary, relay])


__all__ = ["ManagedRuntimeHermesAgentPluginExtension"]
