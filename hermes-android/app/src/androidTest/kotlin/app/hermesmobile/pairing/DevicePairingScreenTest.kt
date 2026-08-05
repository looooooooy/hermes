package app.hermesmobile.pairing

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.text.AnnotatedString
import app.hermesmobile.protocol.pairing.PairingActivationState
import app.hermesmobile.protocol.pairing.PairingConnectorReview
import app.hermesmobile.protocol.pairing.PairingDeviceId
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingOwnerBinding
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingScope
import app.hermesmobile.protocol.pairing.PairingSessionId
import app.hermesmobile.protocol.pairing.PairingSessionState
import app.hermesmobile.ui.theme.HermesMobileTheme
import org.junit.Rule
import org.junit.Test

class DevicePairingScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun manualOwnerClaimCollectsCodeBindingAndRequestedScopes() {
        var state by mutableStateOf(DevicePairingUiState())
        var claimCount = 0
        composeRule.setContent {
            HermesMobileTheme {
                DevicePairingScreen(
                    state = state,
                    onBack = {},
                    onPairingCodeChanged = {
                        state = DevicePairingReducer.reduce(
                            state,
                            DevicePairingEvent.PairingCodeChanged(it),
                        )
                    },
                    onWorkspaceIdChanged = {
                        state = DevicePairingReducer.reduce(
                            state,
                            DevicePairingEvent.WorkspaceIdChanged(it),
                        )
                    },
                    onAgentIdChanged = {
                        state = DevicePairingReducer.reduce(
                            state,
                            DevicePairingEvent.AgentIdChanged(it),
                        )
                    },
                    onDeviceDisplayNameChanged = {
                        state = DevicePairingReducer.reduce(
                            state,
                            DevicePairingEvent.DeviceDisplayNameChanged(it),
                        )
                    },
                    onRequestControlScopeChanged = {
                        state = DevicePairingReducer.reduce(
                            state,
                            DevicePairingEvent.RequestControlScopeChanged(it),
                        )
                    },
                    onFingerprintVerificationChanged = {},
                    onClaim = { claimCount += 1 },
                    onConfirm = {},
                    onRejectFingerprint = {},
                    onCancel = {},
                    onRevoke = {},
                    onReset = {},
                )
            }
        }

        composeRule.onNodeWithText("Device pairing").assertIsDisplayed()
        composeRule.onNodeWithText("Claim pairing").assertIsNotEnabled()
        composeRule.onNodeWithTag("pairing-code-input").performTextInput("2ab3c4d5")
        composeRule.onNodeWithTag("pairing-code-input").assert(
            SemanticsMatcher.expectValue(
                SemanticsProperties.EditableText,
                AnnotatedString("2AB3-C4D5"),
            ),
        )
        composeRule.onNodeWithTag("pairing-workspace-input").performTextInput(WORKSPACE_ID)
        composeRule.onNodeWithTag("pairing-agent-input").performTextInput(AGENT_ID)
        composeRule.onNodeWithTag("pairing-device-name-input").performTextInput("Office Mac")
        composeRule.onNodeWithText("Request control permission").performClick()
        composeRule.onNodeWithText("Claim pairing").assertIsEnabled().performClick()

        composeRule.runOnIdle {
            check(claimCount == 1)
            check(state.selectedScopes.contains(PairingScope.SESSION_CONTROL_REQUEST))
        }
    }

    @Test
    fun reviewShowsConnectorTargetScopeFingerprintAndFixedExpiryBeforeConfirmation() {
        var state by mutableStateOf(
            DevicePairingUiState(
                phase = DevicePairingPhase.REVIEW,
                ownerView = ownerView(),
            ),
        )
        var confirmed = false
        var rejected = false
        composeRule.setContent {
            HermesMobileTheme {
                DevicePairingScreen(
                    state = state,
                    onBack = {},
                    onPairingCodeChanged = {},
                    onWorkspaceIdChanged = {},
                    onAgentIdChanged = {},
                    onDeviceDisplayNameChanged = {},
                    onRequestControlScopeChanged = {},
                    onFingerprintVerificationChanged = {
                        state = DevicePairingReducer.reduce(
                            state,
                            DevicePairingEvent.FingerprintVerificationChanged(it),
                        )
                    },
                    onClaim = {},
                    onConfirm = { confirmed = true },
                    onRejectFingerprint = { rejected = true },
                    onCancel = {},
                    onRevoke = {},
                    onReset = {},
                )
            }
        }

        composeRule.onNodeWithText("Hermes Connector").assertIsDisplayed()
        composeRule.onNodeWithText("macos · 1.0.0").assertIsDisplayed()
        composeRule.onNodeWithText("Ed25519").assertIsDisplayed()
        composeRule.onNodeWithText(FINGERPRINT).assertIsDisplayed()
        composeRule.onNodeWithText(WORKSPACE_ID).assertIsDisplayed()
        composeRule.onNodeWithText(AGENT_ID).assertIsDisplayed()
        composeRule.onNodeWithText("Pairing offer ID").assertIsDisplayed()
        composeRule.onNodeWithText("11111111-1111-4111-8111-111111111111").assertIsDisplayed()
        composeRule.onNodeWithText("Pairing session ID").assertIsDisplayed()
        composeRule.onNodeWithText("22222222-2222-4222-8222-222222222222").assertIsDisplayed()
        composeRule.onNodeWithText("Device ID").assertIsDisplayed()
        composeRule.onNodeWithText("77777777-7777-4777-8777-777777777777").assertIsDisplayed()
        composeRule.onNodeWithText("Credential ID").assertIsDisplayed()
        composeRule.onNodeWithText("88888888-8888-4888-8888-888888888888").assertIsDisplayed()
        composeRule.onNodeWithText("Observe sessions").assertIsDisplayed()
        composeRule.onNodeWithText("Request control").assertIsDisplayed()
        composeRule.onNodeWithText("Expires at 2026-08-01T00:05:00Z").assertIsDisplayed()
        composeRule.onNodeWithText("Pairing does not grant Controller status.").assertIsDisplayed()
        composeRule.onNodeWithText("Confirm pairing").assertIsNotEnabled()

        composeRule.onNodeWithText("I verified this fingerprint on the Connector")
            .performScrollTo()
            .performClick()
        composeRule.onNodeWithText("Confirm pairing")
            .performScrollTo()
            .assertIsEnabled()
            .performClick()
        composeRule.onNodeWithText("Fingerprint does not match")
            .performScrollTo()
            .performClick()

        composeRule.runOnIdle {
            check(confirmed)
            check(rejected)
        }
    }

    @Test
    fun awaitingProofOffersTwoStepRevocationWithoutClaimingControl() {
        var revokeCount = 0
        var cancelCount = 0
        composeRule.setContent {
            HermesMobileTheme {
                DevicePairingScreen(
                    state = DevicePairingUiState(
                        phase = DevicePairingPhase.AWAITING_CONNECTOR_PROOF,
                        ownerView = ownerView(
                            state = PairingSessionState.CONFIRMED,
                            activationState = PairingActivationState.AWAITING_PROOF,
                            revision = 3,
                        ),
                    ),
                    onBack = {},
                    onPairingCodeChanged = {},
                    onWorkspaceIdChanged = {},
                    onAgentIdChanged = {},
                    onDeviceDisplayNameChanged = {},
                    onRequestControlScopeChanged = {},
                    onFingerprintVerificationChanged = {},
                    onClaim = {},
                    onConfirm = {},
                    onRejectFingerprint = {},
                    onCancel = { cancelCount += 1 },
                    onRevoke = { revokeCount += 1 },
                    onReset = {},
                )
            }
        }

        composeRule.onNodeWithText("Waiting for Connector proof").assertIsDisplayed()
        composeRule.onNodeWithText("Controller").assertDoesNotExist()
        composeRule.onNodeWithText("Cancel pending pairing").performClick()
        composeRule.onNodeWithText("Revoke device").performClick()
        composeRule.onNodeWithText("Confirm revoke").assertIsDisplayed().performClick()
        composeRule.runOnIdle {
            check(cancelCount == 1)
            check(revokeCount == 1)
        }
    }

    @Test
    fun unavailableClaimIsGenericWhileRateLimitExpiryAndRevocationRemainExplicit() {
        var state by mutableStateOf(
            DevicePairingUiState(
                phase = DevicePairingPhase.ERROR,
                failure = PairingFailure.Contract(PairingErrorCode.PAIRING_CLAIM_UNAVAILABLE),
            ),
        )
        composeRule.setContent {
            HermesMobileTheme {
                DevicePairingScreen(
                    state = state,
                    onBack = {},
                    onPairingCodeChanged = {},
                    onWorkspaceIdChanged = {},
                    onAgentIdChanged = {},
                    onDeviceDisplayNameChanged = {},
                    onRequestControlScopeChanged = {},
                    onFingerprintVerificationChanged = {},
                    onClaim = {},
                    onConfirm = {},
                    onRejectFingerprint = {},
                    onCancel = {},
                    onRevoke = {},
                    onReset = {},
                )
            }
        }

        composeRule.onNodeWithText("Pairing claim unavailable").assertIsDisplayed()
        composeRule.onNodeWithText("Expired").assertDoesNotExist()
        composeRule.onNodeWithText("Cancelled").assertDoesNotExist()

        composeRule.runOnIdle {
            state = DevicePairingUiState(
                phase = DevicePairingPhase.CLAIM_RATE_LIMITED,
                failure = PairingFailure.Contract(
                    PairingErrorCode.PAIRING_CLAIM_RATE_LIMITED,
                    retryAfterSeconds = 120,
                ),
            )
        }
        composeRule.onNodeWithText("Pairing attempts paused for 120 seconds").assertIsDisplayed()

        composeRule.runOnIdle {
            state = DevicePairingUiState(phase = DevicePairingPhase.EXPIRED)
        }
        composeRule.onNodeWithText("Pairing expired").assertIsDisplayed()

        composeRule.runOnIdle {
            state = DevicePairingUiState(
                phase = DevicePairingPhase.REVOKED,
                revokedAt = "2026-08-01T00:30:00Z",
            )
        }
        composeRule.onNodeWithText("Device revoked").assertIsDisplayed()
        composeRule.onNodeWithText("Revoked at 2026-08-01T00:30:00Z").assertIsDisplayed()

        composeRule.runOnIdle {
            state = DevicePairingUiState(
                phase = DevicePairingPhase.BLOCKED,
                ownerView = ownerView(
                    state = PairingSessionState.CONFIRMED,
                    activationState = PairingActivationState.BLOCKED,
                    revision = 4,
                ),
            )
        }
        composeRule.onNodeWithText("Device activation blocked").assertIsDisplayed()
        composeRule.onNodeWithText(
            "This device is no longer active. Create a new Connector offer to pair again.",
        ).assertIsDisplayed()
        composeRule.onNodeWithText("Revoke device").assertDoesNotExist()
        composeRule.onNodeWithText("Cancel pending pairing").assertDoesNotExist()
        composeRule.onNodeWithText("Pair another device").assertIsDisplayed()
    }

    @Test
    fun deliveryUnknownOffersRetryForTheSameFrozenOperation() {
        var retryCount = 0
        var state by mutableStateOf(
            DevicePairingUiState(
                phase = DevicePairingPhase.DELIVERY_UNKNOWN,
                failure = PairingFailure.DeliveryUnknown,
                pendingOperation = PairingOperationKind.CLAIM,
            ),
        )
        composeRule.setContent {
            HermesMobileTheme {
                DevicePairingScreen(
                    state = state,
                    onBack = {},
                    onPairingCodeChanged = {},
                    onWorkspaceIdChanged = {},
                    onAgentIdChanged = {},
                    onDeviceDisplayNameChanged = {},
                    onRequestControlScopeChanged = {},
                    onFingerprintVerificationChanged = {},
                    onClaim = {},
                    onConfirm = {},
                    onRejectFingerprint = {},
                    onCancel = {},
                    onRevoke = {},
                    onReset = {},
                    onRetryPending = { retryCount += 1 },
                )
            }
        }

        composeRule.onNodeWithText("Claim result unknown").assertIsDisplayed()
        composeRule.onNodeWithText("Retry same claim").assertIsDisplayed().performClick()
        composeRule.onNodeWithText("Pair another device").assertDoesNotExist()
        composeRule.runOnIdle { check(retryCount == 1) }

        composeRule.runOnIdle {
            state = state.copy(
                phase = DevicePairingPhase.AUTHENTICATION_REQUIRED,
                failure = PairingFailure.AuthenticationRequired,
            )
        }
        composeRule.onNodeWithText("Sign-in required").assertIsDisplayed()
        composeRule.onNodeWithText("Retry same claim").assertIsDisplayed()
        composeRule.onNodeWithText("Pair another device").assertDoesNotExist()
    }

    private fun ownerView(
        state: PairingSessionState = PairingSessionState.CLAIMED,
        activationState: PairingActivationState =
            PairingActivationState.WAITING_OWNER_CONFIRMATION,
        revision: Int = 2,
    ) = PairingOwnerView(
        pairingOfferId = "11111111-1111-4111-8111-111111111111",
        pairingSessionId = PairingSessionId("22222222-2222-4222-8222-222222222222"),
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
            deviceId = PairingDeviceId("77777777-7777-4777-8777-777777777777"),
            credentialId = "88888888-8888-4888-8888-888888888888",
            scopes = setOf(
                PairingScope.SESSION_OBSERVE,
                PairingScope.SESSION_CONTROL_REQUEST,
            ),
        ),
        credentialFingerprint = FINGERPRINT,
        expiresAt = "2026-08-01T00:05:00Z",
        revision = revision,
        deviceRevision = 4,
    )

    private companion object {
        const val WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
        const val AGENT_ID = "66666666-6666-4666-8666-666666666666"
        const val FINGERPRINT = "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    }
}
