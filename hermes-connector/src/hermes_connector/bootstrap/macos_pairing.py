"""Pairing-only macOS composition for pre-runtime device enrollment.

Step 3 device pairing intentionally runs before the managed local runtime exists.
This module therefore validates only the private state and native credential
surfaces required for pairing.  The formal Connector service continues to use
``bootstrap.macos.check_macos_runtime`` and its full gateway/database/runtime
preflight.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from hermes_connector.adapters.cloud.pairing_http import DevicePairingHttpClient
from hermes_connector.adapters.platform.macos.credentials import (
    MacOSKeychainCloudTokenProvider,
)
from hermes_connector.adapters.platform.macos.device_identity import (
    MacOSKeychainDeviceIdentity,
)
from hermes_connector.adapters.platform.macos.instance_identity import (
    MacOSInstanceIdentityStore,
)
from hermes_connector.adapters.platform.macos.keychain import MacOSKeychainSecretStore
from hermes_connector.adapters.platform.macos.keychain_broker import MacOSKeychainBroker
from hermes_connector.adapters.platform.macos.pairing_command_lock import (
    MacOSPairingCommandLock,
)
from hermes_connector.adapters.platform.macos.pairing_projection import (
    MacOSPairedProjectionStore,
    MacOSPairingOfferProjectionStore,
)
from hermes_connector.application.pairing_coordinator import (
    PairingConflict,
    PairingCoordinator,
)
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.ports.pairing import DevicePairingCloudError

if TYPE_CHECKING:
    from hermes_connector.adapters.platform.macos.security_framework import (
        SecurityFrameworkAPIPort,
    )
    from hermes_connector.domain.pairing import (
        PairingCancelDisplay,
        PairingStartDisplay,
        PairingStatusDisplay,
    )

_DEVICE_KEY_SERVICE = "wiki.seaotter.hermes.connector.device-key.v1"
_CLOUD_TOKEN_SERVICE = "wiki.seaotter.hermes.connector.cloud-token.v1"
_PAIRING_OFFER_SERVICE = "wiki.seaotter.hermes.connector.pairing-offer.v1"
_ORPHANED_OFFER_ERRORS = frozenset(
    {
        (404, "PAIRING_NOT_FOUND"),
        (410, "PAIRING_EXPIRED"),
    }
)


class RecoveringPairingCoordinator:
    """Recover only Cloud-proven orphan offers; preserve all live pairings."""

    def __init__(
        self,
        *,
        delegate: PairingCoordinator,
        cloud: DevicePairingHttpClient,
        offer_secret_store: MacOSKeychainSecretStore,
        offer_projection_store: MacOSPairingOfferProjectionStore,
    ) -> None:
        self._delegate = delegate
        self._cloud = cloud
        self._offer_secret_store = offer_secret_store
        self._offer_projection_store = offer_projection_store

    async def start(self) -> PairingStartDisplay:
        try:
            return await self._delegate.start()
        except PairingConflict:
            if not await self._recover_cloud_proven_orphan_offer():
                raise
        return await self._delegate.start()

    async def status(self) -> PairingStatusDisplay:
        return await self._delegate.status()

    async def cancel(self) -> PairingCancelDisplay:
        return await self._delegate.cancel()

    async def _recover_cloud_proven_orphan_offer(self) -> bool:
        projection = await self._offer_projection_store.load()
        if projection is None:
            return False
        raw_secret = await self._offer_secret_store.read_secret()
        if raw_secret is None:
            return False
        try:
            pairing_offer_secret = raw_secret.decode("ascii")
        except UnicodeDecodeError:
            return False
        try:
            status = await self._cloud.get_pairing_offer(
                projection.pairing_offer_id,
                pairing_offer_secret=pairing_offer_secret,
            )
        except DevicePairingCloudError as error:
            if (error.status_code, error.code) not in _ORPHANED_OFFER_ERRORS:
                raise
        else:
            if status.state not in {"cancelled", "expired"}:
                return False
        await self._offer_secret_store.delete_secret_if_matches(
            sha256(raw_secret).digest()
        )
        await self._offer_projection_store.delete_if_matches(
            projection.pairing_offer_id
        )
        return True


@dataclass(frozen=True, slots=True)
class MacOSPairingRuntime:
    coordinator: RecoveringPairingCoordinator
    pairing_http: DevicePairingHttpClient
    keychain_broker: MacOSKeychainBroker

    async def aclose(self) -> None:
        try:
            await self.pairing_http.aclose()
        finally:
            await self.keychain_broker.aclose()


def check_macos_pairing_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    security_api: SecurityFrameworkAPIPort | None = None,
) -> None:
    """Validate only authority required for pairing before Managed Runtime exists."""

    if settings.credential_store != "keychain":
        raise ValueError("pairing requires Keychain credentials")
    _validate_private_state_directory(settings.state_directory)
    MacOSInstanceIdentityStore(settings.instance_state_file).check_path()
    MacOSPairedProjectionStore(settings.paired_projection_file).check_path()
    MacOSPairingOfferProjectionStore(settings.pairing_offer_projection_file).check_path()
    _validate_private_file_if_present(settings.pairing_command_lock_file)
    if security_api is not None:
        security_api.check_available()
    else:
        MacOSKeychainBroker().check_available()


def build_macos_pairing_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    security_api: SecurityFrameworkAPIPort | None = None,
) -> MacOSPairingRuntime:
    """Compose one pairing command without requiring the managed runtime."""

    check_macos_pairing_runtime(settings, security_api=security_api)
    keychain_broker = MacOSKeychainBroker()
    identities = MacOSInstanceIdentityStore(settings.instance_state_file).load_or_create()
    account = f"connector-instance:{identities.connector_instance_id}"
    device_identity = MacOSKeychainDeviceIdentity(
        MacOSKeychainSecretStore(
            service=_DEVICE_KEY_SERVICE,
            account=account,
            broker=keychain_broker,
        )
    )
    offer_secret_store = MacOSKeychainSecretStore(
        service=_PAIRING_OFFER_SERVICE,
        account=account,
        broker=keychain_broker,
    )
    token_store = MacOSKeychainCloudTokenProvider(
        MacOSKeychainSecretStore(
            service=_CLOUD_TOKEN_SERVICE,
            account=account,
            broker=keychain_broker,
        )
    )
    pairing_http = DevicePairingHttpClient(settings.cloud_api_endpoint)
    offer_projection_store = MacOSPairingOfferProjectionStore(
        settings.pairing_offer_projection_file
    )
    delegate = PairingCoordinator(
        connector_instance_id=identities.connector_instance_id,
        display_name=settings.display_name,
        connector_version=settings.connector_version,
        identity=device_identity,
        cloud=pairing_http,
        offer_secret_store=offer_secret_store,
        offer_projection_store=offer_projection_store,
        paired_projection_store=MacOSPairedProjectionStore(
            settings.paired_projection_file
        ),
        token_store=token_store,
        now=lambda: datetime.now(UTC),
        new_idempotency_key=uuid4,
        command_lock=MacOSPairingCommandLock(settings.pairing_command_lock_file),
    )
    coordinator = RecoveringPairingCoordinator(
        delegate=delegate,
        cloud=pairing_http,
        offer_secret_store=offer_secret_store,
        offer_projection_store=offer_projection_store,
    )
    return MacOSPairingRuntime(
        coordinator=coordinator,
        pairing_http=pairing_http,
        keychain_broker=keychain_broker,
    )


def _validate_private_state_directory(path: Path) -> None:
    if not path.is_absolute() or "\x00" in str(path) or _has_symlink_component(path):
        raise ValueError("pairing state directory is unsafe")
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("pairing state directory is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("pairing state directory is unsafe")


def _validate_private_file_if_present(path: Path) -> None:
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("pairing state file reference is unsafe")
    _validate_private_state_directory(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("pairing state file is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("pairing state file is unsafe")


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(metadata.st_mode):
            return True
    return False


__all__ = [
    "MacOSPairingRuntime",
    "RecoveringPairingCoordinator",
    "build_macos_pairing_runtime",
    "check_macos_pairing_runtime",
]
