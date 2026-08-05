from hermes_connector.adapters.platform.availability import PlatformAvailability

AVAILABILITY = PlatformAvailability(
    platform_name="macos",
    available=True,
    capabilities=frozenset(
        {
            "agent_discovery",
            "device_identity",
            "instance_lock",
            "local_gateway_transport",
            "secure_storage",
        }
    ),
)
