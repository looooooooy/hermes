from hermes_connector.adapters.platform.macos import (
    agent_discovery,
    observer_discovery,
    plugin_control_relay,
)


def test_all_role_readers_share_one_exact_descriptor_field_set() -> None:
    assert agent_discovery._DESCRIPTOR_FIELDS is observer_discovery._FIELDS
    assert agent_discovery._DESCRIPTOR_FIELDS is plugin_control_relay._DESCRIPTOR_FIELDS


def test_all_role_readers_share_descriptor_version_two() -> None:
    assert getattr(agent_discovery, "_DESCRIPTOR_VERSION", None) == 2
    assert getattr(observer_discovery, "_DESCRIPTOR_VERSION", None) == 2
    assert getattr(plugin_control_relay, "_DESCRIPTOR_VERSION", None) == 2
