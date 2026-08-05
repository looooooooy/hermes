from hermes_connector.adapters.platform.availability import PlatformAvailability

AVAILABILITY = PlatformAvailability(
    platform_name="linux",
    available=False,
    capabilities=frozenset(),
    unavailable_reason="Linux service and local transport adapters are not implemented",
)
