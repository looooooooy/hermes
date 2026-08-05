"""Runtime contracts that keep the standalone package off host-private imports."""

from __future__ import annotations

import builtins

import pytest

from hermes_agent_plugin.adapters.local_protocol import control_relay


def test_control_endpoint_requires_explicit_host_dispatcher(monkeypatch) -> None:
    """Missing host wiring must fail before importing any Hermes private module."""
    real_import = builtins.__import__

    def reject_private_host_import(
        name, globals=None, locals=None, fromlist=(), level=0
    ):
        if name == "tui_gateway" or name.startswith("tui_gateway."):
            raise AssertionError("standalone plugin imported host-private tui_gateway")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_private_host_import)

    with pytest.raises(TypeError, match="dispatcher"):
        control_relay.start_control_endpoint(authority=object())
