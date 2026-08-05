from hermes_connector.adapters.platform.availability import PlatformAvailability

AVAILABILITY = PlatformAvailability(
    platform_name="windows",
    available=False,
    capabilities=frozenset(),
    unavailable_reason=(
        "Windows service and local transport adapters are not implemented"
    ),
)
