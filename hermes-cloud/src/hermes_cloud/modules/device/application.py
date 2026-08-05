"""Application orchestration for device pairing and Connector credentials."""

from __future__ import annotations

import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final
from uuid import UUID

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.device.domain import (
    PAIRING_CHALLENGE_TTL,
    PAIRING_SESSION_TTL,
    DeviceAuthenticationBinding,
    DeviceAuthenticationChallenge,
    DeviceCredential,
    DeviceCredentialStatus,
    DeviceLifecycleState,
    PairingOffer,
    PairingSnapshot,
    fingerprint_ed25519_public_key,
)
from hermes_cloud.modules.device.http_codec import (
    decode_ed25519_public_key,
    internal_fingerprint_from_public,
    internal_revision_from_public,
    public_fingerprint_from_internal,
    public_revision_from_internal,
)
from hermes_cloud.modules.device.ports import (
    ActivatePairingCommand,
    CancelPairingCommand,
    ClaimPairingCommand,
    ConfirmPairingCommand,
    ConsumeDeviceChallengeCommand,
    CreateDeviceChallengeCommand,
    PairingExpired,
    PairingMutation,
    PairingRepositoryPort,
    RevokeDeviceCommand,
)

_PAIRING_CODE_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SIGNING_PAYLOAD_PREFIX: Final = b"hermes-device-auth-v1\0"
_MAX_CONNECTOR_TOKEN_TTL_SECONDS: Final = 3_600
_IDEMPOTENCY_RETENTION: Final = timedelta(days=1)


class DeviceProofRejected(PermissionError):
    """The device proof is malformed, stale, mismatched, or invalid."""


class DevicePairingService:
    """Keep HTTP-neutral pairing policy above the ORM unit of work."""

    def __init__(
        self,
        *,
        repository: PairingRepositoryPort,
        signing_secret: bytes,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        connector_token_ttl_seconds: int = 900,
    ) -> None:
        if (
            not isinstance(signing_secret, bytes)
            or not 32 <= len(signing_secret) <= 4096
        ):
            raise ValueError("device signing secret size is invalid")
        if not 1 <= connector_token_ttl_seconds <= _MAX_CONNECTOR_TOKEN_TTL_SECONDS:
            raise ValueError("Connector token TTL is outside contract bounds")
        self._repository = repository
        self._signing_secret = signing_secret
        self._now = now
        self._connector_token_ttl_seconds = connector_token_ttl_seconds

    def create_offer(
        self,
        *,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        expected_fields = {
            "connector_instance_id",
            "display_name",
            "platform_family",
            "connector_version",
            "key_algorithm",
            "public_key",
        }
        if set(request) != expected_fields or request.get("key_algorithm") != "Ed25519":
            raise ValueError("pairing offer request is invalid")
        connector_instance_id = self._uuid_text(
            request["connector_instance_id"],
            "connector instance",
        )
        display_name = self._text(request["display_name"], "display name", 128)
        platform = self._text(request["platform_family"], "platform", 32)
        connector_version = self._text(
            request["connector_version"],
            "connector version",
            64,
        )
        public_key_value = request["public_key"]
        if not isinstance(public_key_value, str):
            raise TypeError("public key must be text")
        public_key = decode_ed25519_public_key(public_key_value)
        request_digest = self._request_digest(
            method="POST",
            path="/api/device-pairing/offers",
            principal=f"connector:{connector_instance_id}",
            body=request,
        )
        now = self._aware_now()
        pairing_offer_id = self._derived_uuid(
            "pairing-offer-id",
            idempotency_key,
            request_digest,
        )
        pairing_code = self._pairing_code(idempotency_key, request_digest)
        offer_secret_bytes = self._derive(
            "pairing-offer-secret",
            idempotency_key,
            request_digest,
            32,
        )
        offer_secret = _encode_base64url(offer_secret_bytes)
        fingerprint = fingerprint_ed25519_public_key(public_key)
        offer = PairingOffer(
            pairing_offer_id=pairing_offer_id,
            pairing_code_digest=_digest_text(pairing_code),
            bootstrap_secret_digest=sha256(offer_secret_bytes).hexdigest(),
            algorithm="ed25519",
            public_key=public_key,
            credential_fingerprint=fingerprint,
            key_id=fingerprint,
            device_key=connector_instance_id,
            device_name=display_name,
            platform=platform,
            connector_version=connector_version,
            state=PairingSessionState.PENDING,
            revision=0,
            expires_at=now + PAIRING_SESSION_TTL,
            claimed_at=None,
            created_at=now,
        )
        stored = self._repository.create_offer(
            offer,
            mutation=self._mutation(
                operation="create",
                idempotency_key=idempotency_key,
                principal=f"connector:{connector_instance_id}",
                request_digest=request_digest,
                expected_revision=0,
                now=now,
            ),
        )
        if (
            stored.pairing_offer_id != offer.pairing_offer_id
            or stored.pairing_code_digest != offer.pairing_code_digest
            or stored.bootstrap_secret_digest != offer.bootstrap_secret_digest
            or stored.algorithm != offer.algorithm
            or stored.public_key != offer.public_key
            or stored.credential_fingerprint != offer.credential_fingerprint
            or stored.key_id != offer.key_id
            or stored.device_key != offer.device_key
            or stored.device_name != offer.device_name
            or stored.platform != offer.platform
            or stored.connector_version != offer.connector_version
        ):
            raise RuntimeError("pairing offer replay does not match request")
        return {
            "pairing_offer_id": str(stored.pairing_offer_id),
            "pairing_code": pairing_code,
            "pairing_offer_secret": offer_secret,
            "credential_fingerprint": public_fingerprint_from_internal(
                stored.credential_fingerprint
            ),
            "state": "pending",
            "revision": public_revision_from_internal(stored.revision),
            "ttl_seconds": int(PAIRING_SESSION_TTL.total_seconds()),
            "expires_at": _timestamp(stored.expires_at),
        }

    def get_offer(
        self,
        *,
        pairing_offer_id: UUID,
        pairing_offer_secret: str,
    ) -> dict[str, object]:
        snapshot = self._repository.get_offer(
            pairing_offer_id,
            bootstrap_secret_digest=self._secret_digest(pairing_offer_secret),
            now=self._aware_now(),
        )
        return self._connector_pairing_view(snapshot)

    def claim_offer(
        self,
        *,
        principal: Principal,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        pairing_code = self._text(request.get("pairing_code"), "pairing code", 9)
        workspace_id = UUID(self._uuid_text(request.get("workspace_id"), "workspace"))
        agent_id = UUID(self._uuid_text(request.get("agent_id"), "agent"))
        display_name = self._text(
            request.get("device_display_name"),
            "device display name",
            128,
        )
        scopes = self._scopes(request.get("scopes"))
        expected_revision = internal_revision_from_public(
            self._integer(request.get("expected_revision"), "expected revision")
        )
        principal_text = f"owner:{principal.tenant_id}:{principal.user_id}"
        request_digest = self._request_digest(
            method="POST",
            path="/api/device-pairing/claims",
            principal=principal_text,
            body=request,
        )
        now = self._aware_now()
        snapshot = self._repository.claim_offer(
            ClaimPairingCommand(
                pairing_session_id=self._derived_uuid(
                    "pairing-session-id",
                    idempotency_key,
                    request_digest,
                ),
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=self._derived_uuid(
                    "device-id",
                    idempotency_key,
                    request_digest,
                ),
                device_display_name=display_name,
                scopes=scopes,
                pairing_code_digest=_digest_text(pairing_code),
                expected_revision=expected_revision,
                now=now,
            ),
            mutation=self._mutation(
                operation="claim",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=expected_revision,
                now=now,
            ),
        )
        return self._owner_pairing_view(snapshot)

    def get_pairing_session(
        self,
        *,
        principal: Principal,
        pairing_session_id: UUID,
    ) -> dict[str, object]:
        now = self._aware_now()
        snapshot = self._repository.get_owner_pairing_status(
            tenant_id=principal.tenant_id,
            owner_user_id=principal.user_id,
            pairing_session_id=pairing_session_id,
        )
        return self._owner_pairing_view(snapshot, now=now)

    def confirm_pairing(
        self,
        *,
        principal: Principal,
        pairing_session_id: UUID,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        principal_text = f"owner:{principal.tenant_id}:{principal.user_id}"
        path = f"/api/device-pairing/sessions/{pairing_session_id}/confirm"
        request_digest = self._request_digest(
            method="POST",
            path=path,
            principal=principal_text,
            body=request,
        )
        now = self._aware_now()
        replay = self._repository.replay_pairing_mutation(
            self._mutation(
                operation="confirm",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=0,
                now=now,
            )
        )
        if replay is not None:
            return self._owner_pairing_view(replay)
        fingerprint_value = request.get("credential_fingerprint")
        if not isinstance(fingerprint_value, str):
            raise TypeError("credential fingerprint must be text")
        fingerprint = internal_fingerprint_from_public(fingerprint_value)
        expected_revision = internal_revision_from_public(
            self._integer(request.get("expected_revision"), "expected revision")
        )
        current = self._repository.get_owner_pairing(
            tenant_id=principal.tenant_id,
            owner_user_id=principal.user_id,
            pairing_session_id=pairing_session_id,
            now=now,
        )
        challenge_id = self._derived_uuid(
            "initial-device-challenge-id",
            idempotency_key,
            request_digest,
        )
        payload = self.signing_payload_for_challenge(challenge_id)
        snapshot = self._repository.confirm_owner(
            ConfirmPairingCommand(
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                pairing_session_id=pairing_session_id,
                credential_fingerprint=fingerprint,
                expected_revision=expected_revision,
                challenge_id=challenge_id,
                challenge_digest=sha256(payload).hexdigest(),
                challenge_expires_at=min(
                    now + PAIRING_CHALLENGE_TTL,
                    current.offer.expires_at,
                ),
                now=now,
            ),
            mutation=self._mutation(
                operation="confirm",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=expected_revision,
                now=now,
            ),
        )
        return self._owner_pairing_view(snapshot)

    def cancel_pairing(
        self,
        *,
        principal: Principal,
        pairing_session_id: UUID,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        expected_revision = internal_revision_from_public(
            self._integer(request.get("expected_revision"), "expected revision")
        )
        principal_text = f"owner:{principal.tenant_id}:{principal.user_id}"
        path = f"/api/device-pairing/sessions/{pairing_session_id}/cancel"
        request_digest = self._request_digest(
            method="POST",
            path=path,
            principal=principal_text,
            body=request,
        )
        now = self._aware_now()
        snapshot = self._repository.cancel_pairing(
            CancelPairingCommand(
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                pairing_session_id=pairing_session_id,
                expected_revision=expected_revision,
                now=now,
            ),
            mutation=self._mutation(
                operation="cancel",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=expected_revision,
                now=now,
            ),
        )
        return self._owner_pairing_view(snapshot)

    def prove_pairing(
        self,
        *,
        pairing_session_id: UUID,
        pairing_offer_secret: str,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        now = self._aware_now()
        secret_digest = self._secret_digest(pairing_offer_secret)
        snapshot = self._repository.get_pairing_for_proof_history(
            pairing_session_id,
            bootstrap_secret_digest=secret_digest,
        )
        principal_text = f"pairing-offer:{snapshot.offer.pairing_offer_id}"
        path = f"/api/device-pairing/sessions/{pairing_session_id}/proof"
        request_digest = self._request_digest(
            method="POST",
            path=path,
            principal=principal_text,
            body=request,
        )
        expected_revision = 2
        mutation = self._mutation(
            operation="proof",
            idempotency_key=idempotency_key,
            principal=principal_text,
            request_digest=request_digest,
            expected_revision=expected_revision,
            now=now,
        )
        credential_id = self._binding_uuid(
            "credential-id",
            snapshot.offer.pairing_offer_id,
        )
        replay = self._repository.replay_pairing_mutation(mutation)
        if replay is not None:
            return self._token_response(
                replay,
                credential_id=credential_id,
                token_id=self._derived_uuid(
                    "initial-connector-token-id",
                    idempotency_key,
                    request_digest,
                ),
            )
        if snapshot.offer.expires_at <= now:
            raise PairingExpired("pairing offer is expired")
        material = snapshot.material
        session = snapshot.session
        lifecycle = snapshot.lifecycle
        if (
            material is None
            or session is None
            or lifecycle is None
            or material.challenge_id is None
            or material.challenge_digest is None
        ):
            raise DeviceProofRejected("device proof rejected")
        challenge_id = self._request_uuid(request, "challenge_id")
        if challenge_id != material.challenge_id:
            raise DeviceProofRejected("device proof rejected")
        signing_payload, signature = self._proof_values(request)
        self.verify_device_proof(
            public_key=material.public_key,
            challenge_id=challenge_id,
            challenge_digest=material.challenge_digest,
            signing_payload=signing_payload,
            signature=signature,
        )
        activated = self._repository.activate_verified_credential(
            ActivatePairingCommand(
                tenant_id=session.tenant_id,
                pairing_offer_id=snapshot.offer.pairing_offer_id,
                pairing_session_id=pairing_session_id,
                bootstrap_secret_digest=secret_digest,
                challenge_id=challenge_id,
                challenge_digest=material.challenge_digest,
                confirmation_digest=sha256(_decode_base64url(signature)).hexdigest(),
                credential=DeviceCredential(
                    tenant_id=session.tenant_id,
                    credential_id=credential_id,
                    device_id=lifecycle.device_id,
                    algorithm="ed25519",
                    key_id=material.key_id,
                    public_key=material.public_key,
                    credential_fingerprint=material.credential_fingerprint,
                    status=DeviceCredentialStatus.ACTIVE,
                    issued_at=now,
                    expires_at=None,
                    revoked_at=None,
                ),
                expected_revision=expected_revision,
                now=now,
            ),
            mutation=mutation,
        )
        return self._token_response(
            activated,
            credential_id=credential_id,
            token_id=self._derived_uuid(
                "initial-connector-token-id",
                idempotency_key,
                request_digest,
            ),
        )

    def create_device_challenge(
        self,
        *,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        device_id = self._request_uuid(request, "device_id")
        credential_id = self._request_uuid(request, "credential_id")
        now = self._aware_now()
        active = self._repository.active_device_binding(
            tenant_id=None,
            device_id=device_id,
            credential_id=credential_id,
            now=now,
        )
        principal_text = (
            f"device:{active.binding.tenant_id}:{device_id}:{credential_id}"
        )
        request_digest = self._request_digest(
            method="POST",
            path="/api/device-auth/challenges",
            principal=principal_text,
            body=request,
        )
        challenge_id = self._derived_uuid(
            "device-challenge-id",
            idempotency_key,
            request_digest,
        )
        payload = self.signing_payload_for_challenge(challenge_id)
        challenge = DeviceAuthenticationChallenge(
            tenant_id=active.binding.tenant_id,
            challenge_id=challenge_id,
            device_id=device_id,
            credential_id=credential_id,
            challenge_digest=sha256(payload).hexdigest(),
            issued_at=now,
            expires_at=now + PAIRING_CHALLENGE_TTL,
            consumed_at=None,
        )
        stored = self._repository.create_device_challenge(
            CreateDeviceChallengeCommand(challenge=challenge, now=now),
            mutation=self._mutation(
                operation="device_challenge",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=0,
                now=now,
            ),
        )
        if stored.challenge is None:
            raise RuntimeError("device challenge result is unavailable")
        return self._challenge_response(stored.challenge)

    def mint_connector_token(
        self,
        *,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        device_id = self._request_uuid(request, "device_id")
        credential_id = self._request_uuid(request, "credential_id")
        challenge_id = self._request_uuid(request, "challenge_id")
        now = self._aware_now()
        current = self._repository.get_device_challenge(
            device_id=device_id,
            credential_id=credential_id,
            challenge_id=challenge_id,
            now=now,
        )
        challenge = current.challenge
        if challenge is None:
            raise DeviceProofRejected("device proof rejected")
        signing_payload, signature = self._proof_values(request)
        self.verify_device_proof(
            public_key=current.binding.public_key,
            challenge_id=challenge_id,
            challenge_digest=challenge.challenge_digest,
            signing_payload=signing_payload,
            signature=signature,
        )
        principal_text = (
            f"device:{current.binding.tenant_id}:{device_id}:{credential_id}"
        )
        request_digest = self._request_digest(
            method="POST",
            path="/api/device-auth/tokens",
            principal=principal_text,
            body=request,
        )
        consumed = self._repository.consume_device_challenge(
            ConsumeDeviceChallengeCommand(
                device_id=device_id,
                credential_id=credential_id,
                challenge_id=challenge_id,
                challenge_digest=challenge.challenge_digest,
                proof_digest=sha256(_decode_base64url(signature)).hexdigest(),
                now=now,
            ),
            mutation=self._mutation(
                operation="device_token",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=0,
                now=now,
            ),
        )
        return self._token_response_from_binding(
            consumed.binding,
            token_id=self._derived_uuid(
                "connector-token-id",
                idempotency_key,
                request_digest,
            ),
            issued_at=(
                consumed.challenge.consumed_at
                if consumed.challenge is not None
                else None
            ),
        )

    def revoke_device(
        self,
        *,
        principal: Principal,
        device_id: UUID,
        request: Mapping[str, object],
        idempotency_key: UUID,
    ) -> dict[str, object]:
        expected_revision = internal_revision_from_public(
            self._integer(request.get("expected_revision"), "expected revision")
        )
        principal_text = f"owner:{principal.tenant_id}:{principal.user_id}"
        path = f"/api/devices/{device_id}/revoke"
        request_digest = self._request_digest(
            method="POST",
            path=path,
            principal=principal_text,
            body=request,
        )
        now = self._aware_now()
        snapshot = self._repository.revoke_device(
            RevokeDeviceCommand(
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                device_id=device_id,
                now=now,
            ),
            mutation=self._mutation(
                operation="revoke",
                idempotency_key=idempotency_key,
                principal=principal_text,
                request_digest=request_digest,
                expected_revision=expected_revision,
                now=now,
            ),
        )
        if snapshot.lifecycle is None:
            raise RuntimeError("revoked lifecycle is unavailable")
        return {
            "device_id": str(snapshot.lifecycle.device_id),
            "status": "revoked",
            "revision": public_revision_from_internal(snapshot.lifecycle.revision),
            "revoked_at": _timestamp(snapshot.lifecycle.updated_at),
        }

    def signing_payload_for_challenge(self, challenge_id: UUID) -> bytes:
        """Reconstruct a challenge without storing its plaintext."""

        challenge = hmac.digest(
            self._signing_secret,
            b"device-challenge\0" + challenge_id.bytes,
            "sha256",
        )
        return _SIGNING_PAYLOAD_PREFIX + challenge_id.bytes + challenge

    def verify_device_proof(
        self,
        *,
        public_key: bytes,
        challenge_id: UUID,
        challenge_digest: str,
        signing_payload: str,
        signature: str,
    ) -> None:
        expected_payload = self.signing_payload_for_challenge(challenge_id)
        try:
            payload = _decode_base64url(signing_payload)
            signature_bytes = _decode_base64url(signature)
            if (
                len(public_key) != 32
                or len(signature_bytes) != 64
                or not payload.startswith(_SIGNING_PAYLOAD_PREFIX)
                or not hmac.compare_digest(payload, expected_payload)
                or not hmac.compare_digest(
                    sha256(payload).hexdigest(),
                    challenge_digest,
                )
            ):
                raise ValueError
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature_bytes,
                payload,
            )
        except (InvalidSignature, TypeError, ValueError):
            raise DeviceProofRejected("device proof rejected") from None

    def issue_connector_token(
        self,
        *,
        tenant_id: UUID,
        device_id: UUID,
        credential_id: UUID,
        agent_id: UUID,
        scopes: Sequence[str],
        token_id: UUID,
        issued_at: datetime | None = None,
    ) -> str:
        if (
            not scopes
            or len(scopes) != len(set(scopes))
            or not set(scopes) <= {"session.observe", "session.control.request"}
        ):
            raise ValueError("Connector token scopes are invalid")
        token_issued_at = issued_at or self._aware_now()
        if token_issued_at.utcoffset() is None:
            raise ValueError("Connector token issue time must include a timezone")
        now = int(token_issued_at.astimezone(UTC).timestamp())
        return jwt.encode(
            {
                "tenant_id": str(tenant_id),
                "device_id": str(device_id),
                "credential_id": str(credential_id),
                "agent_id": str(agent_id),
                "scopes": list(scopes),
                "jti": str(token_id),
                "iat": now,
                "nbf": now,
                "exp": now + self._connector_token_ttl_seconds,
            },
            self._signing_secret,
            algorithm="HS256",
        )

    def _owner_pairing_view(
        self,
        snapshot: PairingSnapshot,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        session = snapshot.session
        material = snapshot.material
        lifecycle = snapshot.lifecycle
        if session is None or material is None or lifecycle is None:
            raise RuntimeError("owner pairing view is unavailable")
        state = session.state.value
        if (
            now is not None
            and session.expires_at <= now
            and lifecycle.state is DeviceLifecycleState.PENDING
            and session.state
            not in {
                PairingSessionState.CANCELLED,
                PairingSessionState.EXPIRED,
            }
        ):
            state = PairingSessionState.EXPIRED.value
        if lifecycle.state.value == "active":
            activation_state = "active"
        elif (
            state
            in {
                PairingSessionState.CANCELLED.value,
                PairingSessionState.EXPIRED.value,
            }
            or lifecycle.state is not DeviceLifecycleState.PENDING
        ):
            activation_state = "blocked"
        elif session.state is PairingSessionState.CONFIRMED:
            activation_state = "awaiting_proof"
        else:
            activation_state = "waiting_owner_confirmation"
        return {
            "pairing_offer_id": str(snapshot.offer.pairing_offer_id),
            "pairing_session_id": str(session.pairing_session_id),
            "state": state,
            "activation_state": activation_state,
            "binding": {
                "tenant_id": str(session.tenant_id),
                "user_id": str(material.claimed_by_user_id),
                "workspace_id": str(session.workspace_id),
                "agent_id": str(session.agent_id),
                "device_id": str(lifecycle.device_id),
                "credential_id": str(
                    self._binding_uuid(
                        "credential-id",
                        snapshot.offer.pairing_offer_id,
                    )
                ),
                "scopes": list(material.scopes),
            },
            "display_name": snapshot.offer.device_name,
            "platform_family": snapshot.offer.platform,
            "connector_version": snapshot.offer.connector_version,
            "key_algorithm": "Ed25519",
            "credential_fingerprint": public_fingerprint_from_internal(
                snapshot.offer.credential_fingerprint
            ),
            "expires_at": _timestamp(snapshot.offer.expires_at),
            "revision": public_revision_from_internal(material.revision),
            "device_revision": public_revision_from_internal(lifecycle.revision),
        }

    def _connector_pairing_view(
        self,
        snapshot: PairingSnapshot,
    ) -> dict[str, object]:
        offer = snapshot.offer
        base: dict[str, object] = {
            "pairing_offer_id": str(offer.pairing_offer_id),
            "expires_at": _timestamp(offer.expires_at),
        }
        if snapshot.session is None:
            return {
                **base,
                "state": offer.state.value,
                "activation_state": (
                    "waiting_owner"
                    if offer.state is PairingSessionState.PENDING
                    else "blocked"
                ),
                "revision": public_revision_from_internal(offer.revision),
            }
        session = snapshot.session
        material = snapshot.material
        lifecycle = snapshot.lifecycle
        if material is None or lifecycle is None:
            raise RuntimeError("connector pairing view is unavailable")
        base["pairing_session_id"] = str(session.pairing_session_id)
        if session.state is PairingSessionState.CLAIMED:
            return {
                **base,
                "state": "claimed",
                "activation_state": "waiting_owner_confirmation",
                "revision": public_revision_from_internal(material.revision),
            }
        if session.state in {
            PairingSessionState.CANCELLED,
            PairingSessionState.EXPIRED,
        }:
            return {
                **base,
                "state": session.state.value,
                "activation_state": "blocked",
                "revision": public_revision_from_internal(material.revision),
            }
        binding = {
            "tenant_id": str(session.tenant_id),
            "device_id": str(lifecycle.device_id),
            "credential_id": str(
                self._binding_uuid("credential-id", offer.pairing_offer_id)
            ),
            "agent_id": str(session.agent_id),
            "scopes": list(material.scopes),
        }
        if lifecycle.state.value == "active":
            return {
                **base,
                "state": "confirmed",
                "activation_state": "active",
                "binding": binding,
                "revision": public_revision_from_internal(material.revision),
            }
        if material.challenge_id is None or material.challenge_expires_at is None:
            raise RuntimeError("pairing challenge is unavailable")
        return {
            **base,
            "state": "confirmed",
            "activation_state": "awaiting_proof",
            "binding": binding,
            "challenge": self._challenge_response(
                DeviceAuthenticationChallenge(
                    tenant_id=session.tenant_id,
                    challenge_id=material.challenge_id,
                    device_id=lifecycle.device_id,
                    credential_id=self._binding_uuid(
                        "credential-id",
                        offer.pairing_offer_id,
                    ),
                    challenge_digest=material.challenge_digest or "",
                    issued_at=material.owner_confirmed_at or material.updated_at,
                    expires_at=material.challenge_expires_at,
                    consumed_at=None,
                )
            ),
            "revision": public_revision_from_internal(material.revision),
        }

    def _challenge_response(
        self,
        challenge: DeviceAuthenticationChallenge,
    ) -> dict[str, object]:
        ttl_seconds = max(
            1,
            min(
                60,
                int((challenge.expires_at - challenge.issued_at).total_seconds()),
            ),
        )
        return {
            "challenge_id": str(challenge.challenge_id),
            "signing_payload": _encode_base64url(
                self.signing_payload_for_challenge(challenge.challenge_id)
            ),
            "ttl_seconds": ttl_seconds,
            "expires_at": _timestamp(challenge.expires_at),
        }

    def _token_response(
        self,
        snapshot: PairingSnapshot,
        *,
        credential_id: UUID,
        token_id: UUID,
    ) -> dict[str, object]:
        session = snapshot.session
        material = snapshot.material
        lifecycle = snapshot.lifecycle
        if session is None or material is None or lifecycle is None:
            raise RuntimeError("Connector token binding is unavailable")
        if snapshot.credential is None:
            raise RuntimeError("Connector token credential is unavailable")
        return self._token_response_from_binding(
            DeviceAuthenticationBinding(
                tenant_id=session.tenant_id,
                device_id=lifecycle.device_id,
                credential_id=credential_id,
                workspace_id=session.workspace_id,
                agent_id=session.agent_id,
                scopes=material.scopes,
                public_key=material.public_key,
                lifecycle_state=lifecycle.state,
                lifecycle_revision=lifecycle.revision,
                credential_status=DeviceCredentialStatus.ACTIVE,
                credential_expires_at=None,
            ),
            token_id=token_id,
            issued_at=snapshot.credential.issued_at,
        )

    def _token_response_from_binding(
        self,
        binding: DeviceAuthenticationBinding,
        *,
        token_id: UUID,
        issued_at: datetime | None,
    ) -> dict[str, object]:
        tenant_id = binding.tenant_id
        device_id = binding.device_id
        credential_id = binding.credential_id
        agent_id = binding.agent_id
        scopes = binding.scopes
        token = self.issue_connector_token(
            tenant_id=tenant_id,
            device_id=device_id,
            credential_id=credential_id,
            agent_id=agent_id,
            scopes=scopes,
            token_id=token_id,
            issued_at=issued_at,
        )
        token_issued_at = issued_at or self._aware_now()
        expires_at = token_issued_at + timedelta(
            seconds=self._connector_token_ttl_seconds
        )
        return {
            "access_token": token,
            "token_type": "Bearer",
            "ttl_seconds": self._connector_token_ttl_seconds,
            "expires_at": _timestamp(expires_at),
            "binding": {
                "tenant_id": str(tenant_id),
                "device_id": str(device_id),
                "credential_id": str(credential_id),
                "agent_id": str(agent_id),
                "scopes": list(scopes),
            },
        }

    def _binding_uuid(self, label: str, pairing_offer_id: UUID) -> UUID:
        raw = bytearray(
            hmac.digest(
                self._signing_secret,
                label.encode("ascii") + b"\0" + pairing_offer_id.bytes,
                "sha256",
            )[:16]
        )
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return UUID(bytes=bytes(raw))

    @staticmethod
    def _proof_values(request: Mapping[str, object]) -> tuple[str, str]:
        if request.get("signature_algorithm") != "Ed25519":
            raise DeviceProofRejected("device proof rejected")
        signing_payload = request.get("signing_payload")
        signature = request.get("signature")
        if not isinstance(signing_payload, str) or not isinstance(signature, str):
            raise DeviceProofRejected("device proof rejected")
        return signing_payload, signature

    @staticmethod
    def _request_uuid(request: Mapping[str, object], field: str) -> UUID:
        value = request.get(field)
        if not isinstance(value, str):
            raise TypeError(f"{field} must be text")
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError(f"{field} is invalid") from None
        if str(parsed) != value:
            raise ValueError(f"{field} is invalid")
        return parsed

    @staticmethod
    def _integer(value: object, field: str) -> int:
        if type(value) is not int:
            raise TypeError(f"{field} must be an integer")
        return value

    @staticmethod
    def _scopes(value: object) -> tuple[str, ...]:
        if (
            not isinstance(value, list)
            or not value
            or len(value) > 2
            or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError("pairing scopes are invalid")
        scopes = tuple(value)
        if len(scopes) != len(set(scopes)) or not set(scopes) <= {
            "session.observe",
            "session.control.request",
        }:
            raise ValueError("pairing scopes are invalid")
        return scopes

    @staticmethod
    def _secret_digest(value: str) -> str:
        secret = _decode_base64url(value)
        if len(secret) != 32:
            raise ValueError("pairing offer secret is invalid")
        return sha256(secret).hexdigest()

    def _mutation(
        self,
        *,
        operation: str,
        idempotency_key: UUID,
        principal: str,
        request_digest: str,
        expected_revision: int,
        now: datetime,
    ) -> PairingMutation:
        return PairingMutation(
            pairing_mutation_id=self._derived_uuid(
                f"{operation}-mutation-id",
                idempotency_key,
                request_digest,
            ),
            operation=operation,
            idempotency_key_digest=_digest_text(str(idempotency_key)),
            principal_digest=_digest_text(principal),
            request_digest=request_digest,
            expected_revision=expected_revision,
            created_at=now,
            expires_at=now + _IDEMPOTENCY_RETENTION,
        )

    def _request_digest(
        self,
        *,
        method: str,
        path: str,
        principal: str,
        body: Mapping[str, object],
    ) -> str:
        canonical = json.dumps(
            {
                "body": body,
                "method": method,
                "path": path,
                "principal": principal,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _pairing_code(
        self,
        idempotency_key: UUID,
        request_digest: str,
    ) -> str:
        bits = int.from_bytes(
            self._derive(
                "pairing-code",
                idempotency_key,
                request_digest,
                5,
            ),
            "big",
        )
        encoded = "".join(
            _PAIRING_CODE_ALPHABET[(bits >> shift) & 31] for shift in range(35, -1, -5)
        )
        return f"{encoded[:4]}-{encoded[4:]}"

    def _derived_uuid(
        self,
        label: str,
        idempotency_key: UUID,
        request_digest: str,
    ) -> UUID:
        raw = bytearray(self._derive(label, idempotency_key, request_digest, 16))
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return UUID(bytes=bytes(raw))

    def _derive(
        self,
        label: str,
        idempotency_key: UUID,
        request_digest: str,
        length: int,
    ) -> bytes:
        seed = (
            label.encode("ascii")
            + b"\0"
            + idempotency_key.bytes
            + bytes.fromhex(request_digest)
        )
        blocks = bytearray()
        counter = 0
        while len(blocks) < length:
            blocks.extend(
                hmac.digest(
                    self._signing_secret,
                    seed + counter.to_bytes(4, "big"),
                    "sha256",
                )
            )
            counter += 1
        return bytes(blocks[:length])

    def _aware_now(self) -> datetime:
        now = self._now()
        if now.utcoffset() is None:
            raise RuntimeError("pairing clock must include a timezone")
        return now.astimezone(UTC)

    @staticmethod
    def _uuid_text(value: object, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be text")
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError(f"{field} is invalid") from None
        if str(parsed) != value:
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _text(value: object, field: str, max_bytes: int) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be text")
        if (
            not value.strip()
            or value != value.strip()
            or len(value.encode("utf-8")) > max_bytes
        ):
            raise ValueError(f"{field} is invalid")
        return value


def _digest_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _encode_base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError("base64url value is invalid")
    decoded = urlsafe_b64decode(f"{value}{'=' * (-len(value) % 4)}")
    if _encode_base64url(decoded) != value:
        raise ValueError("base64url value is not canonical")
    return decoded


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
