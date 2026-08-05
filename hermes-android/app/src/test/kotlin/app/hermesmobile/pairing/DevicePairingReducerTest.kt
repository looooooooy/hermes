package app.hermesmobile.pairing

import app.hermesmobile.protocol.pairing.PairingActivationState
import app.hermesmobile.protocol.pairing.PairingConnectorReview
import app.hermesmobile.protocol.pairing.PairingDeviceId
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingOwnerBinding
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingScope
import app.hermesmobile.protocol.pairing.PairingSessionId
import app.hermesmobile.protocol.pairing.PairingSessionState
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class DevicePairingReducerTest {
    @Test
    fun `manual code input is uppercased grouped and never appears in state diagnostics`() {
        val state = DevicePairingReducer.reduce(
            DevicePairingUiState(),
            DevicePairingEvent.PairingCodeChanged("2ab3 c4d5"),
        )

        assertEquals("2AB3-C4D5", state.pairingCodeInput)
        assertFalse(state.toString().contains("2AB3-C4D5"))
    }

    @Test
    fun `claim is gated by code owner targets device label and observe scope`() {
        val ready = validEntryState()

        assertTrue(ready.canClaim)
        assertFalse(ready.requestControlScope)
        assertEquals(setOf(PairingScope.SESSION_OBSERVE), ready.selectedScopes)
        assertFalse(
            DevicePairingReducer.reduce(
                ready,
                DevicePairingEvent.WorkspaceIdChanged("not-a-uuid"),
            ).canClaim,
        )
    }

    @Test
    fun `claim start clears one-time code and success enters fingerprint review`() {
        val started = DevicePairingReducer.reduce(
            validEntryState(),
            DevicePairingEvent.ClaimStarted,
        )
        assertEquals(DevicePairingPhase.CLAIMING, started.phase)
        assertEquals("", started.pairingCodeInput)

        val reviewed = DevicePairingReducer.reduce(
            started,
            DevicePairingEvent.OwnerViewReceived(ownerView()),
        )
        assertEquals(DevicePairingPhase.REVIEW, reviewed.phase)
        assertFalse(reviewed.fingerprintVerified)
        assertFalse(reviewed.canConfirm)
        assertEquals(FINGERPRINT, reviewed.ownerView?.credentialFingerprint)
        assertEquals("2026-08-01T00:05:00Z", reviewed.fixedExpiresAt)
    }

    @Test
    fun `owner must explicitly verify fingerprint before confirmation`() {
        val review = DevicePairingReducer.reduce(
            DevicePairingUiState(phase = DevicePairingPhase.CLAIMING),
            DevicePairingEvent.OwnerViewReceived(ownerView()),
        )
        assertFalse(review.canConfirm)

        val verified = DevicePairingReducer.reduce(
            review,
            DevicePairingEvent.FingerprintVerificationChanged(true),
        )
        assertTrue(verified.canConfirm)

        val confirming = DevicePairingReducer.reduce(
            verified,
            DevicePairingEvent.ConfirmStarted,
        )
        assertEquals(DevicePairingPhase.CONFIRMING, confirming.phase)
        assertFalse(confirming.canConfirm)
    }

    @Test
    fun `confirmed pairing waits for connector proof and never claims controller ownership`() {
        val state = DevicePairingReducer.reduce(
            DevicePairingUiState(phase = DevicePairingPhase.CONFIRMING),
            DevicePairingEvent.OwnerViewReceived(
                ownerView(
                    state = PairingSessionState.CONFIRMED,
                    activationState = PairingActivationState.AWAITING_PROOF,
                    revision = 3,
                ),
            ),
        )

        assertEquals(DevicePairingPhase.AWAITING_CONNECTOR_PROOF, state.phase)
        assertTrue(state.canRevoke)
        assertFalse(state.grantsController)
    }

    @Test
    fun `confirmed blocked activation is terminal and cannot be cancelled or revoked`() {
        val state = DevicePairingReducer.reduce(
            DevicePairingUiState(phase = DevicePairingPhase.AWAITING_CONNECTOR_PROOF),
            DevicePairingEvent.OwnerViewReceived(
                ownerView(
                    state = PairingSessionState.CONFIRMED,
                    activationState = PairingActivationState.BLOCKED,
                    revision = 4,
                ),
            ),
        )

        assertEquals(DevicePairingPhase.BLOCKED, state.phase)
        assertFalse(state.canCancel)
        assertFalse(state.canRevoke)
        assertFalse(state.grantsController)
    }

    @Test
    fun `contract terminal errors become explicit owner statuses`() {
        val expired = DevicePairingReducer.reduce(
            DevicePairingUiState(phase = DevicePairingPhase.CLAIMING),
            DevicePairingEvent.Failed(PairingFailure.Contract(PairingErrorCode.PAIRING_EXPIRED)),
        )
        val attempts = DevicePairingReducer.reduce(
            DevicePairingUiState(phase = DevicePairingPhase.CLAIMING),
            DevicePairingEvent.Failed(
                PairingFailure.Contract(
                    PairingErrorCode.PAIRING_CLAIM_RATE_LIMITED,
                    retryAfterSeconds = 120,
                ),
            ),
        )
        val auth = DevicePairingReducer.reduce(
            DevicePairingUiState(phase = DevicePairingPhase.CLAIMING),
            DevicePairingEvent.Failed(PairingFailure.AuthenticationRequired),
        )

        assertEquals(DevicePairingPhase.EXPIRED, expired.phase)
        assertEquals(DevicePairingPhase.CLAIM_RATE_LIMITED, attempts.phase)
        assertEquals(DevicePairingPhase.AUTHENTICATION_REQUIRED, auth.phase)
    }

    @Test
    fun `cancelled and revoked responses are terminal and clear review confirmation`() {
        val cancelled = DevicePairingReducer.reduce(
            DevicePairingUiState(
                phase = DevicePairingPhase.CANCELLING,
                fingerprintVerified = true,
            ),
            DevicePairingEvent.OwnerViewReceived(
                ownerView(
                    state = PairingSessionState.CANCELLED,
                    activationState = PairingActivationState.BLOCKED,
                    revision = 3,
                ),
            ),
        )
        val revoked = DevicePairingReducer.reduce(
            DevicePairingUiState(
                phase = DevicePairingPhase.REVOKING,
                ownerView = ownerView(),
                fingerprintVerified = true,
            ),
            DevicePairingEvent.DeviceRevoked(
                deviceId = DEVICE_ID,
                revokedAt = "2026-08-01T00:30:00Z",
            ),
        )

        assertEquals(DevicePairingPhase.CANCELLED, cancelled.phase)
        assertFalse(cancelled.fingerprintVerified)
        assertEquals(DevicePairingPhase.REVOKED, revoked.phase)
        assertNull(revoked.ownerView)
        assertEquals("2026-08-01T00:30:00Z", revoked.revokedAt)
    }

    private fun validEntryState(): DevicePairingUiState {
        var state = DevicePairingUiState()
        state = DevicePairingReducer.reduce(
            state,
            DevicePairingEvent.PairingCodeChanged("2AB3-C4D5"),
        )
        state = DevicePairingReducer.reduce(
            state,
            DevicePairingEvent.WorkspaceIdChanged(WORKSPACE_ID),
        )
        state = DevicePairingReducer.reduce(
            state,
            DevicePairingEvent.AgentIdChanged(AGENT_ID),
        )
        return DevicePairingReducer.reduce(
            state,
            DevicePairingEvent.DeviceDisplayNameChanged("Office Mac"),
        )
    }

    private fun ownerView(
        state: PairingSessionState = PairingSessionState.CLAIMED,
        activationState: PairingActivationState =
            PairingActivationState.WAITING_OWNER_CONFIRMATION,
        revision: Int = 2,
    ) = PairingOwnerView(
        pairingOfferId = "11111111-1111-4111-8111-111111111111",
        pairingSessionId = PairingSessionId(SESSION_ID),
        state = state,
        activationState = activationState,
        connector = PairingConnectorReview(
            displayName = "Hermes Connector",
            platformFamily = "macos",
            version = "1.0.0",
            keyAlgorithm = "Ed25519",
        ),
        binding = PairingOwnerBinding(
            tenantId = "33333333-3333-4333-8333-333333333333",
            userId = "44444444-4444-4444-8444-444444444444",
            workspaceId = WORKSPACE_ID,
            agentId = AGENT_ID,
            deviceId = PairingDeviceId(DEVICE_ID),
            credentialId = "88888888-8888-4888-8888-888888888888",
            scopes = setOf(PairingScope.SESSION_OBSERVE),
        ),
        credentialFingerprint = FINGERPRINT,
        expiresAt = "2026-08-01T00:05:00Z",
        revision = revision,
        deviceRevision = 4,
    )

    private companion object {
        const val WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
        const val AGENT_ID = "66666666-6666-4666-8666-666666666666"
        const val SESSION_ID = "22222222-2222-4222-8222-222222222222"
        const val DEVICE_ID = "77777777-7777-4777-8777-777777777777"
        const val FINGERPRINT = "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    }
}
