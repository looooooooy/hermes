"""Redacted macOS Keychain adapter errors."""

from hermes_connector.ports.secure_storage import SecureStorageError


class KeychainSecretUnavailable(SecureStorageError):
    """The Keychain reference, item, or helper process is unavailable."""


class KeychainBrokerEffectUnknown(KeychainSecretUnavailable):
    """A terminated mutation could not be reconciled by a fresh helper."""
