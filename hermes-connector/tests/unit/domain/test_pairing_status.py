from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from hermes_connector.domain.pairing import (
    DeviceAuthenticationChallenge,
    DeviceBinding,
    PairingOfferStatus,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
OFFER_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
BINDING = DeviceBinding(
    tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
    device_id=UUID("77777777-7777-4777-8777-777777777777"),
    credential_id=UUID("88888888-8888-4888-8888-888888888888"),
    agent_id=UUID("99999999-9999-4999-8999-999999999999"),
    scopes=("session.observe",),
)
CHALLENGE = DeviceAuthenticationChallenge(
    challenge_id=UUID("55555555-5555-4555-8555-555555555555"),
    signing_payload="A" * 64,
    ttl_seconds=60,
    expires_at=NOW,
)


@pytest.mark.parametrize(
    "values",
    (
        {
            "state": "pending",
            "activation_state": "waiting_owner",
            "pairing_session_id": SESSION_ID,
            "binding": None,
            "challenge": None,
        },
        {
            "state": "claimed",
            "activation_state": "waiting_owner_confirmation",
            "pairing_session_id": None,
            "binding": None,
            "challenge": None,
        },
        {
            "state": "confirmed",
            "activation_state": "awaiting_proof",
            "pairing_session_id": SESSION_ID,
            "binding": BINDING,
            "challenge": None,
        },
        {
            "state": "confirmed",
            "activation_state": "active",
            "pairing_session_id": SESSION_ID,
            "binding": BINDING,
            "challenge": CHALLENGE,
        },
        {
            "state": "expired",
            "activation_state": "blocked",
            "pairing_session_id": None,
            "binding": BINDING,
            "challenge": None,
        },
    ),
)
def test_pairing_status_rejects_impossible_state_field_combinations(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="pairing status"):
        PairingOfferStatus(
            pairing_offer_id=OFFER_ID,
            expires_at=NOW,
            revision=1,
            **values,  # type: ignore[arg-type]
        )
