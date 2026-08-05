"""Fail-closed compatibility tombstones for the retired standalone runtime.

Production Plugin resources are owned exclusively by the running Hermes Agent
through the published ``hermes_agent.plugins`` entry point.  Keeping these
names as hard failures gives older callers an actionable diagnostic without
preserving a path that can invent a second runtime generation or endpoint set.
"""

from __future__ import annotations

_MESSAGE = (
    "standalone_plugin_runtime_prohibited: load hermes-agent-plugin through "
    "the running Hermes Agent PluginManager; gateway-extension/1 is required"
)


class StandaloneRuntimeProhibited(RuntimeError):
    """Raised before a caller can create a non-authoritative Plugin runtime."""


def _prohibited(*_args: object, **_kwargs: object) -> None:
    raise StandaloneRuntimeProhibited(_MESSAGE)


class HermesAgentPluginRuntime:
    """Retired compatibility name; the Agent Host owns the only runtime."""

    def __new__(cls, *_args: object, **_kwargs: object) -> HermesAgentPluginRuntime:
        _prohibited()
        raise AssertionError("unreachable")


GatewayBootstrap = HermesAgentPluginRuntime
create_platform_local_gateway_resource = _prohibited
create_production_control_relay_resource = _prohibited
_new_runtime_generation = _prohibited

__all__ = ["StandaloneRuntimeProhibited"]
