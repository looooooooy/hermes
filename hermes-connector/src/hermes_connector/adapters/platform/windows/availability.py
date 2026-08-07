from hermes_connector.adapters.platform.availability import PlatformAvailability

# Single-instance locking plus same-user Local Gateway discovery/handshake are
# implemented. Keep the platform unavailable until observer/control relay and
# Windows service lifecycle are completed and tested end-to-end.
AVAILABILITY = PlatformAvailability(
    platform_name="windows",
    available=False,
    capabilities=frozenset(
        {
            "instance_lock",
            "local_gateway.discovery",
            "local_gateway.handshake",
        }
    ),
    unavailable_reason=(
        "Windows observer/control relay and service lifecycle are not implemented"
    ),
)
