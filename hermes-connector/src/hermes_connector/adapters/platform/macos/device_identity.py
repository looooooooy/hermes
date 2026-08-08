"""macOS compatibility name for secure-store-backed device identity."""

from __future__ import annotations

from hermes_connector.adapters.secure_store_device_identity import (
    SecureStoreDeviceIdentity,
    UnsafeDeviceIdentity,
)


class MacOSKeychainDeviceIdentity(SecureStoreDeviceIdentity):
    """Compatibility wrapper preserving the established macOS adapter name."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MacOSKeychainDeviceIdentity(<private-key-redacted>)"


__all__ = ["MacOSKeychainDeviceIdentity", "UnsafeDeviceIdentity"]
