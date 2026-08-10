from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from hermes_connector.application.pairing_coordinator import PairingConflict
from hermes_connector.bootstrap.macos_pairing import (
    RecoveringPairingCoordinator,
    check_macos_pairing_runtime,
)
from hermes_connector.ports.pairing import DevicePairingCloudError


class _SecurityAPI:
    def __init__(self) -> None:
        self.checks = 0

    def check_available(self) -> None:
        self.checks += 1


@dataclass
class _PairingSettings:
    credential_store: str
    state_directory: Path
    instance_state_file: Path
    paired_projection_file: Path
    pairing_offer_projection_file: Path
    pairing_command_lock_file: Path


def test_pairing_preflight_does_not_require_managed_runtime_gateway_or_database(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    settings = _PairingSettings(
        credential_store="keychain",
        state_directory=state,
        instance_state_file=state / "instances.json",
        paired_projection_file=state / "paired.json",
        pairing_offer_projection_file=state / "pairing-offer.json",
        pairing_command_lock_file=state / "pairing-command.lock",
    )
    security = _SecurityAPI()

    check_macos_pairing_runtime(settings, security_api=security)  # type: ignore[arg-type]

    assert security.checks == 1
    assert not settings.instance_state_file.exists()
    assert not settings.paired_projection_file.exists()
    assert not settings.pairing_offer_projection_file.exists()
    assert not settings.pairing_command_lock_file.exists()


class _Delegate:
    def __init__(self, *, recoverable: bool) -> None:
        self.recoverable = recoverable
        self.starts = 0
        self.result = object()

    async def start(self):
        self.starts += 1
        if self.starts == 1:
            raise PairingConflict("connector already has an active pairing offer")
        return self.result

    async def status(self):
        raise AssertionError("status not used")

    async def cancel(self):
        raise AssertionError("cancel not used")


@dataclass(frozen=True)
class _Projection:
    pairing_offer_id: UUID


class _ProjectionStore:
    def __init__(self) -> None:
        self.projection = _Projection(UUID("11111111-1111-4111-8111-111111111111"))
        self.deleted: list[UUID] = []

    async def load(self):
        return self.projection

    async def delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        self.deleted.append(pairing_offer_id)
        return True


class _SecretStore:
    def __init__(self) -> None:
        self.secret = b"A" * 43
        self.deleted = []

    async def read_secret(self):
        return self.secret

    async def delete_secret_if_matches(self, digest: bytes) -> bool:
        self.deleted.append(digest)
        return True


class _MissingCloudOffer:
    async def get_pairing_offer(self, *_args, **_kwargs):
        raise DevicePairingCloudError(code="PAIRING_NOT_FOUND", status_code=404)


class _LiveCloudOffer:
    class _Status:
        state = "pending"

    async def get_pairing_offer(self, *_args, **_kwargs):
        return self._Status()


@pytest.mark.asyncio
async def test_cloud_proven_orphan_offer_is_cleaned_and_pairing_restarts() -> None:
    delegate = _Delegate(recoverable=True)
    projection_store = _ProjectionStore()
    secret_store = _SecretStore()
    coordinator = RecoveringPairingCoordinator(
        delegate=delegate,  # type: ignore[arg-type]
        cloud=_MissingCloudOffer(),  # type: ignore[arg-type]
        offer_secret_store=secret_store,  # type: ignore[arg-type]
        offer_projection_store=projection_store,  # type: ignore[arg-type]
    )

    result = await coordinator.start()

    assert result is delegate.result
    assert delegate.starts == 2
    assert projection_store.deleted == [projection_store.projection.pairing_offer_id]
    assert len(secret_store.deleted) == 1


@pytest.mark.asyncio
async def test_live_cloud_offer_is_never_deleted_during_recovery() -> None:
    delegate = _Delegate(recoverable=False)
    projection_store = _ProjectionStore()
    secret_store = _SecretStore()
    coordinator = RecoveringPairingCoordinator(
        delegate=delegate,  # type: ignore[arg-type]
        cloud=_LiveCloudOffer(),  # type: ignore[arg-type]
        offer_secret_store=secret_store,  # type: ignore[arg-type]
        offer_projection_store=projection_store,  # type: ignore[arg-type]
    )

    with pytest.raises(PairingConflict):
        await coordinator.start()

    assert delegate.starts == 1
    assert projection_store.deleted == []
    assert secret_store.deleted == []
