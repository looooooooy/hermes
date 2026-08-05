"""Shared runtime authority v2 fixture for macOS transport tests."""

from __future__ import annotations

from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    MacOSRuntimeAuthorityV2,
    capture_macos_host_authority,
)


def runtime_authority_v2(
    *,
    profile: str = "default",
    runtime_generation: str = "runtime-generation-1",
) -> MacOSRuntimeAuthorityV2:
    return capture_macos_host_authority(
        profile=profile,
        host_bundle_id="com.nousresearch.hermes",
    ).bind_runtime(runtime_generation)


__all__ = ["runtime_authority_v2"]
