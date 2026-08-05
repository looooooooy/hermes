from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos.pairing_command_lock import (
    MacOSPairingCommandLock,
)
from hermes_connector.application.pairing_coordinator import (
    PairingConflict,
    PairingCoordinator,
    PairingProtocolViolation,
)
from hermes_connector.domain.pairing import (
    ConnectorToken,
    DeviceAuthenticationChallenge,
    DeviceBinding,
    DeviceChallengeProof,
    PairedProjection,
    PairingCancelDisplay,
    PairingOffer,
    PairingOfferProjection,
    PairingOfferRequest,
    PairingOfferStatus,
    PairingStartDisplay,
    PairingStatusDisplay,
)
from hermes_connector.ports.device_identity import DevicePublicIdentity
from hermes_connector.ports.pairing import DevicePairingCloudError

NOW = datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)
CONNECTOR_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PAIRING_OFFER_ID = UUID("22222222-2222-4222-8222-222222222222")
IDEMPOTENCY_KEY = UUID("33333333-3333-4333-8333-333333333333")
PAIRING_SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
CHALLENGE_ID = UUID("55555555-5555-4555-8555-555555555555")
TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")
DEVICE_ID = UUID("77777777-7777-4777-8777-777777777777")
CREDENTIAL_ID = UUID("88888888-8888-4888-8888-888888888888")
AGENT_ID = UUID("99999999-9999-4999-8999-999999999999")
PUBLIC_IDENTITY = DevicePublicIdentity(
    key_handle="hermes-device-key:v1:fingerprint",
    algorithm="Ed25519",
    public_key="A" * 43,
    fingerprint="SHA256:" + "B" * 43,
)


class _Identity:
    def __init__(self) -> None:
        self.sign_calls: list[tuple[str, bytes]] = []

    async def get_or_create(self) -> DevicePublicIdentity:
        return PUBLIC_IDENTITY

    async def sign_challenge(self, key_handle: str, challenge: bytes) -> bytes:
        self.sign_calls.append((key_handle, challenge))
        return b"\x01" * 64


class _Cloud:
    def __init__(self) -> None:
        self.create_calls: list[tuple[PairingOfferRequest, UUID]] = []
        self.status_response: PairingOfferStatus | None = None
        self.status_calls: list[tuple[UUID, str]] = []
        self.proof_response: ConnectorToken | None = None
        self.proof_calls: list[tuple[UUID, DeviceChallengeProof, str, UUID]] = []
        self.status_error: DevicePairingCloudError | None = None

    async def create_pairing_offer(
        self,
        request: PairingOfferRequest,
        *,
        idempotency_key: UUID,
    ) -> PairingOffer:
        self.create_calls.append((request, idempotency_key))
        return PairingOffer(
            pairing_offer_id=PAIRING_OFFER_ID,
            pairing_code="ABCD-EFGH",
            pairing_offer_secret="S" * 43,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            state="pending",
            revision=1,
            ttl_seconds=300,
            expires_at=NOW + timedelta(seconds=300),
        )

    async def get_pairing_offer(
        self,
        pairing_offer_id: UUID,
        *,
        pairing_offer_secret: str,
    ) -> PairingOfferStatus:
        self.status_calls.append((pairing_offer_id, pairing_offer_secret))
        if self.status_error is not None:
            raise self.status_error
        assert self.status_response is not None
        return self.status_response

    async def prove_pairing_session(
        self,
        pairing_session_id: UUID,
        proof: DeviceChallengeProof,
        *,
        pairing_offer_secret: str,
        idempotency_key: UUID,
    ) -> ConnectorToken:
        self.proof_calls.append(
            (
                pairing_session_id,
                proof,
                pairing_offer_secret,
                idempotency_key,
            )
        )
        assert self.proof_response is not None
        return self.proof_response


class _SecretStore:
    def __init__(self, value: bytes | None = None) -> None:
        self.value = value
        self.deleted = 0
        self.compared_digests: list[bytes] = []

    async def write_secret(self, secret: bytes) -> None:
        self.value = secret

    async def read_secret(self) -> bytes | None:
        return self.value

    async def delete_secret(self) -> bool:
        existed = self.value is not None
        self.value = None
        self.deleted += 1
        return existed

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        self.compared_digests.append(expected_sha256)
        if self.value is None:
            return False
        if hashlib.sha256(self.value).digest() != expected_sha256:
            return False
        self.value = None
        self.deleted += 1
        return True


class _OfferProjectionStore:
    def __init__(self, saved: PairingOfferProjection | None = None) -> None:
        self.saved = saved
        self.deleted = 0

    async def save(self, _projection: object) -> None:
        assert isinstance(_projection, PairingOfferProjection)
        self.saved = _projection

    async def load(self) -> object | None:
        return self.saved

    async def delete(self) -> bool:
        existed = self.saved is not None
        self.saved = None
        self.deleted += 1
        return existed

    async def delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        if self.saved is None or self.saved.pairing_offer_id != pairing_offer_id:
            return False
        self.saved = None
        self.deleted += 1
        return True


class _PairedProjectionStore:
    def __init__(self, saved: PairedProjection | None = None) -> None:
        self.saved = saved

    async def load(self) -> object | None:
        return self.saved

    async def save(self, _projection: object) -> None:
        assert isinstance(_projection, PairedProjection)
        self.saved = _projection


class _TokenStore:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.cleared = 0

    async def store_access_token(self, _token: str) -> None:
        self.tokens.append(_token)

    async def clear_access_token(self) -> None:
        self.tokens.clear()
        self.cleared += 1


@pytest.mark.asyncio
async def test_start_creates_tenant_neutral_offer_from_public_identity() -> None:
    cloud = _Cloud()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=_SecretStore(),
        offer_projection_store=_OfferProjectionStore(),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    await coordinator.start()

    assert cloud.create_calls == [
        (
            PairingOfferRequest(
                connector_instance_id=CONNECTOR_INSTANCE_ID,
                display_name="Office Mac",
                platform_family="macos",
                connector_version="1.2.3",
                key_algorithm="Ed25519",
                public_key="A" * 43,
            ),
            IDEMPOTENCY_KEY,
        )
    ]
    assert "tenant" not in PairingOfferRequest.__dataclass_fields__
    assert "device" not in PairingOfferRequest.__dataclass_fields__
    assert "agent" not in PairingOfferRequest.__dataclass_fields__


@pytest.mark.asyncio
async def test_start_persists_secret_only_in_temporary_store_and_returns_safe_display() -> (
    None
):
    cloud = _Cloud()
    secret_store = _SecretStore()
    offer_store = _OfferProjectionStore()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.start()

    assert result == PairingStartDisplay(
        pairing_code="ABCD-EFGH",
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
    )
    assert secret_store.value == ("S" * 43).encode("ascii")
    assert offer_store.saved == PairingOfferProjection(
        pairing_offer_id=PAIRING_OFFER_ID,
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
    )
    assert "ABCD-EFGH" not in repr(result)
    assert "S" * 43 not in repr(result)
    assert "ABCD-EFGH" not in repr(offer_store.saved)
    assert "S" * 43 not in repr(offer_store.saved)


@pytest.mark.asyncio
async def test_status_polls_with_offer_secret_and_returns_pending_progress() -> None:
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=None,
        state="pending",
        activation_state="waiting_owner",
        binding=None,
        challenge=None,
        expires_at=NOW + timedelta(seconds=300),
        revision=2,
    )
    projection = PairingOfferProjection(
        pairing_offer_id=PAIRING_OFFER_ID,
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
    )
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(projection)
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert cloud.status_calls == [(PAIRING_OFFER_ID, "S" * 43)]
    assert result == PairingStatusDisplay(
        state="pending",
        activation_state="waiting_owner",
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
        revision=2,
    )
    assert secret_store.deleted == 0
    assert offer_store.deleted == 0
    assert "S" * 43 not in repr(result)


@pytest.mark.asyncio
async def test_status_rejects_changed_offer_expiry_before_signing() -> None:
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=None,
        state="pending",
        activation_state="waiting_owner",
        binding=None,
        challenge=None,
        expires_at=NOW + timedelta(seconds=299),
        revision=2,
    )
    identity = _Identity()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=identity,
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(PairingProtocolViolation, match="expiry"):
        await coordinator.status()

    assert identity.sign_calls == []
    assert cloud.proof_calls == []


@pytest.mark.asyncio
async def test_status_rejects_challenge_that_outlives_offer_before_signing() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"x" * 40
    signing_payload = base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode()
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        state="confirmed",
        activation_state="awaiting_proof",
        binding=binding,
        challenge=DeviceAuthenticationChallenge(
            challenge_id=CHALLENGE_ID,
            signing_payload=signing_payload,
            ttl_seconds=60,
            expires_at=NOW + timedelta(seconds=301),
        ),
        expires_at=NOW + timedelta(seconds=300),
        revision=4,
    )
    identity = _Identity()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=identity,
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(PairingProtocolViolation, match="outlives"):
        await coordinator.status()

    assert identity.sign_calls == []
    assert cloud.proof_calls == []


@pytest.mark.asyncio
async def test_status_410_pairing_expired_cleans_local_offer_and_returns_blocked() -> (
    None
):
    cloud = _Cloud()
    cloud.status_error = DevicePairingCloudError(
        code="PAIRING_EXPIRED",
        status_code=410,
    )
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(
        PairingOfferProjection(
            pairing_offer_id=PAIRING_OFFER_ID,
            key_handle=PUBLIC_IDENTITY.key_handle,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            expires_at=NOW + timedelta(seconds=300),
        )
    )
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert result == PairingStatusDisplay(
        state="expired",
        activation_state="blocked",
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
        revision=None,
    )
    assert secret_store.deleted == 1
    assert offer_store.deleted == 1


@pytest.mark.asyncio
async def test_pairing_operations_are_serialized_in_process() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingCloud(_Cloud):
        async def create_pairing_offer(
            self,
            request: PairingOfferRequest,
            *,
            idempotency_key: UUID,
        ) -> PairingOffer:
            entered.set()
            await release.wait()
            return await super().create_pairing_offer(
                request,
                idempotency_key=idempotency_key,
            )

    secret_store = _SecretStore()
    offer_store = _OfferProjectionStore()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=_BlockingCloud(),
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    start_task = asyncio.create_task(coordinator.start())
    await entered.wait()
    cancel_task = asyncio.create_task(coordinator.cancel())
    await asyncio.sleep(0)

    assert cancel_task.done() is False
    release.set()
    await start_task
    await cancel_task
    assert secret_store.value is None
    assert offer_store.saved is None


@pytest.mark.asyncio
async def test_two_independent_coordinators_create_only_one_offer(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingCloud(_Cloud):
        async def create_pairing_offer(
            self,
            request: PairingOfferRequest,
            *,
            idempotency_key: UUID,
        ) -> PairingOffer:
            self.create_calls.append((request, idempotency_key))
            entered.set()
            await release.wait()
            return PairingOffer(
                pairing_offer_id=PAIRING_OFFER_ID,
                pairing_code="ABCD-EFGH",
                pairing_offer_secret="S" * 43,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                state="pending",
                revision=1,
                ttl_seconds=300,
                expires_at=NOW + timedelta(seconds=300),
            )

    tmp_path.chmod(0o700)
    cloud = _BlockingCloud()
    secret_store = _SecretStore()
    offer_store = _OfferProjectionStore()
    common = {
        "connector_instance_id": CONNECTOR_INSTANCE_ID,
        "display_name": "Office Mac",
        "connector_version": "1.2.3",
        "identity": _Identity(),
        "cloud": cloud,
        "offer_secret_store": secret_store,
        "offer_projection_store": offer_store,
        "paired_projection_store": _PairedProjectionStore(),
        "token_store": _TokenStore(),
        "now": lambda: NOW,
        "new_idempotency_key": lambda: IDEMPOTENCY_KEY,
    }
    lock_path = tmp_path / "pairing-command.lock"
    first = PairingCoordinator(
        **common,
        command_lock=MacOSPairingCommandLock(lock_path),
    )
    second = PairingCoordinator(
        **common,
        command_lock=MacOSPairingCommandLock(lock_path),
    )

    first_task = asyncio.create_task(first.start())
    await entered.wait()
    second_task = asyncio.create_task(second.start())
    await asyncio.sleep(0.1)

    assert second_task.done() is False
    assert len(cloud.create_calls) == 1
    release.set()
    first_result = await first_task
    second_result = await asyncio.gather(second_task, return_exceptions=True)

    assert first_result.pairing_code == "ABCD-EFGH"
    assert isinstance(second_result[0], PairingConflict)
    assert len(cloud.create_calls) == 1


@pytest.mark.asyncio
async def test_independent_status_and_cancel_commands_do_not_interleave(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    projection = PairingOfferProjection(
        pairing_offer_id=PAIRING_OFFER_ID,
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
    )

    class _BlockingCloud(_Cloud):
        async def get_pairing_offer(
            self,
            pairing_offer_id: UUID,
            *,
            pairing_offer_secret: str,
        ) -> PairingOfferStatus:
            entered.set()
            await release.wait()
            return PairingOfferStatus(
                pairing_offer_id=pairing_offer_id,
                pairing_session_id=None,
                state="pending",
                activation_state="waiting_owner",
                binding=None,
                challenge=None,
                expires_at=projection.expires_at,
                revision=2,
            )

    tmp_path.chmod(0o700)
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(projection)
    common = {
        "connector_instance_id": CONNECTOR_INSTANCE_ID,
        "display_name": "Office Mac",
        "connector_version": "1.2.3",
        "identity": _Identity(),
        "cloud": _BlockingCloud(),
        "offer_secret_store": secret_store,
        "offer_projection_store": offer_store,
        "paired_projection_store": _PairedProjectionStore(),
        "token_store": _TokenStore(),
        "now": lambda: NOW,
        "new_idempotency_key": lambda: IDEMPOTENCY_KEY,
    }
    lock_path = tmp_path / "pairing-command.lock"
    status_coordinator = PairingCoordinator(
        **common,
        command_lock=MacOSPairingCommandLock(lock_path),
    )
    cancel_coordinator = PairingCoordinator(
        **common,
        command_lock=MacOSPairingCommandLock(lock_path),
    )

    status_task = asyncio.create_task(status_coordinator.status())
    await entered.wait()
    cancel_task = asyncio.create_task(cancel_coordinator.cancel())
    await asyncio.sleep(0.1)

    assert cancel_task.done() is False
    assert secret_store.value == ("S" * 43).encode("ascii")
    assert offer_store.saved == projection
    release.set()
    assert (await status_task).state == "pending"
    assert (await cancel_task).state == "cancelled_local"
    assert secret_store.value is None
    assert offer_store.saved is None


@pytest.mark.asyncio
async def test_stale_terminal_cleanup_preserves_concurrently_replaced_offer() -> None:
    original_projection = PairingOfferProjection(
        pairing_offer_id=PAIRING_OFFER_ID,
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=300),
    )
    replacement_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    replacement_projection = PairingOfferProjection(
        pairing_offer_id=replacement_id,
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW + timedelta(seconds=600),
    )
    original_secret = ("S" * 43).encode("ascii")
    replacement_secret = ("R" * 43).encode("ascii")
    secret_store = _SecretStore(original_secret)
    offer_store = _OfferProjectionStore(original_projection)

    class _ReplacingCloud(_Cloud):
        async def get_pairing_offer(
            self,
            pairing_offer_id: UUID,
            *,
            pairing_offer_secret: str,
        ) -> PairingOfferStatus:
            secret_store.value = replacement_secret
            offer_store.saved = replacement_projection
            return PairingOfferStatus(
                pairing_offer_id=PAIRING_OFFER_ID,
                pairing_session_id=None,
                state="expired",
                activation_state="blocked",
                binding=None,
                challenge=None,
                expires_at=original_projection.expires_at,
                revision=3,
            )

    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=_ReplacingCloud(),
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert result.state == "expired"
    assert secret_store.value == replacement_secret
    assert offer_store.saved == replacement_projection
    assert secret_store.compared_digests == [hashlib.sha256(original_secret).digest()]


@pytest.mark.asyncio
async def test_status_deletes_expired_temporary_secret_without_polling() -> None:
    cloud = _Cloud()
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(
        PairingOfferProjection(
            pairing_offer_id=PAIRING_OFFER_ID,
            key_handle=PUBLIC_IDENTITY.key_handle,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            expires_at=NOW,
        )
    )
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert result == PairingStatusDisplay(
        state="expired",
        activation_state="blocked",
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        expires_at=NOW,
        revision=None,
    )
    assert cloud.status_calls == []
    assert secret_store.deleted == 1
    assert offer_store.deleted == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ("expired", "cancelled"))
async def test_status_deletes_temporary_secret_on_server_terminal_state(
    terminal_state: str,
) -> None:
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=None,
        state=terminal_state,
        activation_state="blocked",
        binding=None,
        challenge=None,
        expires_at=NOW + timedelta(seconds=300),
        revision=3,
    )
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(
        PairingOfferProjection(
            pairing_offer_id=PAIRING_OFFER_ID,
            key_handle=PUBLIC_IDENTITY.key_handle,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            expires_at=NOW + timedelta(seconds=300),
        )
    )
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert result.state == terminal_state
    assert result.activation_state == "blocked"
    assert secret_store.deleted == 1
    assert offer_store.deleted == 1


@pytest.mark.asyncio
async def test_confirmed_status_signs_server_payload_and_persists_binding() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"x" * 40
    signing_payload = base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode()
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe", "session.control.request"),
    )
    challenge = DeviceAuthenticationChallenge(
        challenge_id=CHALLENGE_ID,
        signing_payload=signing_payload,
        ttl_seconds=60,
        expires_at=NOW + timedelta(seconds=60),
    )
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        state="confirmed",
        activation_state="awaiting_proof",
        binding=binding,
        challenge=challenge,
        expires_at=NOW + timedelta(seconds=300),
        revision=4,
    )
    cloud.proof_response = ConnectorToken(
        access_token="T" * 64,
        token_type="Bearer",
        ttl_seconds=300,
        expires_at=NOW + timedelta(seconds=300),
        binding=binding,
    )
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(
        PairingOfferProjection(
            pairing_offer_id=PAIRING_OFFER_ID,
            key_handle=PUBLIC_IDENTITY.key_handle,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            expires_at=NOW + timedelta(seconds=300),
        )
    )
    paired_store = _PairedProjectionStore()
    token_store = _TokenStore()
    identity = _Identity()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=identity,
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=paired_store,
        token_store=token_store,
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert identity.sign_calls == [(PUBLIC_IDENTITY.key_handle, decoded_payload)]
    assert cloud.proof_calls == [
        (
            PAIRING_SESSION_ID,
            DeviceChallengeProof(
                challenge_id=CHALLENGE_ID,
                signing_payload=signing_payload,
                signature_algorithm="Ed25519",
                signature=base64.urlsafe_b64encode(b"\x01" * 64).rstrip(b"=").decode(),
            ),
            "S" * 43,
            IDEMPOTENCY_KEY,
        )
    ]
    assert token_store.tokens == ["T" * 64]
    assert paired_store.saved == PairedProjection(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe", "session.control.request"),
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        token_expires_at=NOW + timedelta(seconds=300),
        lifecycle_state="active",
    )
    assert result.state == "confirmed"
    assert result.activation_state == "active"
    assert secret_store.deleted == 1
    assert offer_store.deleted == 1
    assert "T" * 64 not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signing_payload",
    (
        base64.urlsafe_b64encode(b"wrong-device-domain\x00" + b"x" * 40)
        .rstrip(b"=")
        .decode(),
        (base64.urlsafe_b64encode(b"hermes-device-auth-v1\x00" + b"x" * 40).decode()),
        "A" * 1025,
    ),
)
async def test_confirmed_status_rejects_invalid_signing_payload_before_signing(
    signing_payload: str,
) -> None:
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        state="confirmed",
        activation_state="awaiting_proof",
        binding=binding,
        challenge=DeviceAuthenticationChallenge(
            challenge_id=CHALLENGE_ID,
            signing_payload=signing_payload,
            ttl_seconds=60,
            expires_at=NOW + timedelta(seconds=60),
        ),
        expires_at=NOW + timedelta(seconds=300),
        revision=4,
    )
    identity = _Identity()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=identity,
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(ValueError, match="signing payload"):
        await coordinator.status()

    assert identity.sign_calls == []
    assert cloud.proof_calls == []


@pytest.mark.asyncio
async def test_confirmed_status_rejects_expired_challenge_before_signing() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"x" * 40
    signing_payload = base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode()
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        state="confirmed",
        activation_state="awaiting_proof",
        binding=binding,
        challenge=DeviceAuthenticationChallenge(
            challenge_id=CHALLENGE_ID,
            signing_payload=signing_payload,
            ttl_seconds=60,
            expires_at=NOW,
        ),
        expires_at=NOW + timedelta(seconds=300),
        revision=4,
    )
    identity = _Identity()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=identity,
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(ValueError, match="challenge is expired"):
        await coordinator.status()

    assert identity.sign_calls == []
    assert cloud.proof_calls == []


@pytest.mark.asyncio
async def test_cancel_only_abandons_local_offer_and_deletes_temporary_secret() -> None:
    cloud = _Cloud()
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(
        PairingOfferProjection(
            pairing_offer_id=PAIRING_OFFER_ID,
            key_handle=PUBLIC_IDENTITY.key_handle,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            expires_at=NOW + timedelta(seconds=300),
        )
    )
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.cancel()

    assert result == PairingCancelDisplay(state="cancelled_local")
    assert secret_store.deleted == 1
    assert offer_store.deleted == 1
    assert cloud.status_calls == []


@pytest.mark.asyncio
async def test_start_refuses_to_replace_existing_paired_projection() -> None:
    cloud = _Cloud()
    paired = PairedProjection(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        token_expires_at=NOW + timedelta(seconds=300),
        lifecycle_state="auth_blocked",
    )
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=_SecretStore(),
        offer_projection_store=_OfferProjectionStore(),
        paired_projection_store=_PairedProjectionStore(paired),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(PairingConflict):
        await coordinator.start()

    assert cloud.create_calls == []


@pytest.mark.asyncio
async def test_start_rejects_server_fingerprint_mismatch_without_storing_secret() -> (
    None
):
    class _MismatchedCloud(_Cloud):
        async def create_pairing_offer(
            self,
            request: PairingOfferRequest,
            *,
            idempotency_key: UUID,
        ) -> PairingOffer:
            offer = await super().create_pairing_offer(
                request,
                idempotency_key=idempotency_key,
            )
            return PairingOffer(
                pairing_offer_id=offer.pairing_offer_id,
                pairing_code=offer.pairing_code,
                pairing_offer_secret=offer.pairing_offer_secret,
                credential_fingerprint="SHA256:" + "C" * 43,
                state=offer.state,
                revision=offer.revision,
                ttl_seconds=offer.ttl_seconds,
                expires_at=offer.expires_at,
            )

    secret_store = _SecretStore()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=_MismatchedCloud(),
        offer_secret_store=secret_store,
        offer_projection_store=_OfferProjectionStore(),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(PairingProtocolViolation):
        await coordinator.start()

    assert secret_store.value is None


@pytest.mark.asyncio
async def test_start_refuses_to_replace_an_unexpired_pairing_offer() -> None:
    cloud = _Cloud()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(PairingConflict):
        await coordinator.start()

    assert cloud.create_calls == []


@pytest.mark.asyncio
async def test_start_removes_temporary_secret_when_projection_save_fails() -> None:
    class _FailingOfferStore(_OfferProjectionStore):
        async def save(self, _projection: object) -> None:
            raise OSError("must-never-appear")

    secret_store = _SecretStore()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=_Cloud(),
        offer_secret_store=secret_store,
        offer_projection_store=_FailingOfferStore(),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(OSError):
        await coordinator.start()

    assert secret_store.value is None
    assert secret_store.deleted == 1


@pytest.mark.asyncio
async def test_status_rejects_response_for_a_different_offer() -> None:
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        pairing_session_id=None,
        state="pending",
        activation_state="waiting_owner",
        binding=None,
        challenge=None,
        expires_at=NOW + timedelta(seconds=300),
        revision=2,
    )
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_PairedProjectionStore(),
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(PairingProtocolViolation):
        await coordinator.status()


@pytest.mark.asyncio
async def test_active_status_recovers_binding_and_forces_token_renewal() -> None:
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        state="confirmed",
        activation_state="active",
        binding=binding,
        challenge=None,
        expires_at=NOW + timedelta(seconds=300),
        revision=5,
    )
    secret_store = _SecretStore(("S" * 43).encode("ascii"))
    offer_store = _OfferProjectionStore(
        PairingOfferProjection(
            pairing_offer_id=PAIRING_OFFER_ID,
            key_handle=PUBLIC_IDENTITY.key_handle,
            credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
            expires_at=NOW + timedelta(seconds=300),
        )
    )
    paired_store = _PairedProjectionStore()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=secret_store,
        offer_projection_store=offer_store,
        paired_projection_store=paired_store,
        token_store=_TokenStore(),
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    result = await coordinator.status()

    assert paired_store.saved == PairedProjection(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
        key_handle=PUBLIC_IDENTITY.key_handle,
        credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
        token_expires_at=NOW,
        lifecycle_state="active",
    )
    assert result.activation_state == "active"
    assert secret_store.deleted == 1
    assert offer_store.deleted == 1


@pytest.mark.asyncio
async def test_proof_clears_token_when_projection_commit_fails() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"x" * 40
    signing_payload = base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode()
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.status_response = PairingOfferStatus(
        pairing_offer_id=PAIRING_OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        state="confirmed",
        activation_state="awaiting_proof",
        binding=binding,
        challenge=DeviceAuthenticationChallenge(
            challenge_id=CHALLENGE_ID,
            signing_payload=signing_payload,
            ttl_seconds=60,
            expires_at=NOW + timedelta(seconds=60),
        ),
        expires_at=NOW + timedelta(seconds=300),
        revision=4,
    )
    cloud.proof_response = ConnectorToken(
        access_token="T" * 64,
        token_type="Bearer",
        ttl_seconds=300,
        expires_at=NOW + timedelta(seconds=300),
        binding=binding,
    )

    class _FailingPairedStore(_PairedProjectionStore):
        async def save(self, _projection: object) -> None:
            raise OSError("must-never-appear")

    token_store = _TokenStore()
    coordinator = PairingCoordinator(
        connector_instance_id=CONNECTOR_INSTANCE_ID,
        display_name="Office Mac",
        connector_version="1.2.3",
        identity=_Identity(),
        cloud=cloud,
        offer_secret_store=_SecretStore(("S" * 43).encode("ascii")),
        offer_projection_store=_OfferProjectionStore(
            PairingOfferProjection(
                pairing_offer_id=PAIRING_OFFER_ID,
                key_handle=PUBLIC_IDENTITY.key_handle,
                credential_fingerprint=PUBLIC_IDENTITY.fingerprint,
                expires_at=NOW + timedelta(seconds=300),
            )
        ),
        paired_projection_store=_FailingPairedStore(),
        token_store=token_store,
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(OSError):
        await coordinator.status()

    assert token_store.tokens == []
    assert token_store.cleared == 1
