from hermes_connector.adapters.platform.availability import PlatformAvailability

AVAILABILITY = PlatformAvailability(
    platform_name="windows",
    available=True,
    capabilities=frozenset(
        {
            "control.command",
            "control.owner",
            "device_identity.ed25519",
            "instance_lock",
            "local_gateway.discovery",
            "local_gateway.handshake",
            "local_gateway.preflight",
            "observer",
            "pairing",
            "runtime.cli",
            "runtime.service",
            "runtime.settings",
            "runtime.sqlite.private",
            "secure_state.dpapi",
            "session_catalog",
            "status_receipt",
        }
    ),
    unavailable_reason=None,
)
