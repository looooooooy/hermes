"""Compatibility contract against the real Hermes 0.19 plugin host."""

from __future__ import annotations

import re
import sys
from importlib.metadata import entry_points, version
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HERMES_019_SITE_PACKAGES = (
    PLUGIN_ROOT
    / ".venv"
    / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)
if not HERMES_019_SITE_PACKAGES.is_dir():
    pytest.skip(
        "Hermes 0.19 contract environment is missing: "
        f"{HERMES_019_SITE_PACKAGES} is not a directory; create the project "
        ".venv with the hermes-019-contract-test extra to run these tests",
        allow_module_level=True,
    )
sys.path.insert(0, str(HERMES_019_SITE_PACKAGES))
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from hermes_cli.plugins import (
    PluginContext,
    PluginManager,
    PluginManifest,
)

import hermes_agent_plugin

INCOMPATIBLE_HOST_MESSAGE = (
    "Hermes Agent Host SPI v1 is unavailable; "
    "hermes-agent-plugin requires a host exposing "
    "gateway_extension_spi_version=1 and register_gateway_extension()"
)


def _real_context() -> PluginContext:
    return PluginContext(
        PluginManifest(
            name="hermes-agent-plugin",
            source="entrypoint",
            key="hermes-agent-plugin",
        ),
        PluginManager(),
    )


def test_real_hermes_019_context_fails_closed_with_actionable_error() -> None:
    assert version("hermes-agent") == "0.19.0"
    context = _real_context()
    required_host_facade = {
        "add_runtime_listener",
        "audit",
        "control_snapshot",
        "invoke_owner_action",
        "prepare_observer",
        "register_local_endpoint",
        "runtime_descriptor",
    }
    assert not any(
        hasattr(context, method_name) for method_name in required_host_facade
    )

    with pytest.raises(
        RuntimeError,
        match="^" + re.escape(INCOMPATIBLE_HOST_MESSAGE) + "$",
    ):
        hermes_agent_plugin.register(context)


def test_real_entry_point_discovery_surfaces_the_same_compatibility_error(
    tmp_path,
    monkeypatch,
) -> None:
    assert version("hermes-agent") == "0.19.0"
    plugin_entry_points = [
        entry_point
        for entry_point in entry_points().select(group="hermes_agent.plugins")
        if entry_point.name == "hermes-agent-plugin"
    ]
    assert len(plugin_entry_points) == 1
    assert plugin_entry_points[0].load() is hermes_agent_plugin

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-agent-plugin\n",
        encoding="utf-8",
    )
    bundled_plugins = tmp_path / "bundled-plugins"
    bundled_plugins.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled_plugins))

    manager = PluginManager()
    manager.discover_and_load()
    discovered = {plugin["key"]: plugin for plugin in manager.list_plugins()}[
        "hermes-agent-plugin"
    ]

    assert discovered["source"] == "entrypoint"
    assert discovered["enabled"] is False
    assert discovered["error"] == INCOMPATIBLE_HOST_MESSAGE


def test_real_hermes_019_hooks_and_message_injection_are_not_a_host_spi() -> None:
    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="probe", source="entrypoint"),
        manager,
    )
    observed = []

    hook_registration = context.register_hook(
        "post_llm_call",
        lambda **event: observed.append(event),
    )

    assert hook_registration is None
    manager.invoke_hook("post_llm_call", session_id="session-1")
    assert observed == [
        {
            "session_id": "session-1",
            "telemetry_schema_version": "hermes.observer.v1",
        }
    ]
    assert context.inject_message("must not reach a gateway session") is False
    with pytest.raises(AttributeError):
        _ = context.subagent_lifecycle
