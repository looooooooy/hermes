package app.hermesmobile.pairing

import app.hermesmobile.connection.MainDispatcherRule
import app.hermesmobile.protocol.pairing.CancelPairingRequest
import app.hermesmobile.protocol.pairing.ClaimPairingRequest
import app.hermesmobile.protocol.pairing.ConfirmPairingRequest
import app.hermesmobile.protocol.pairing.DeviceRevokeReason
import app.hermesmobile.protocol.pairing.PairingActivationState
import app.hermesmobile.protocol.pairing.PairingCancelReason
import app.hermesmobile.protocol.pairing.PairingCode
import app.hermesmobile.protocol.pairing.PairingConnectorReview
import app.hermesmobile.protocol.pairing.PairingDeviceId
import app.hermesmobile.protocol.pairing.PairingErrorCode
import app.hermesmobile.protocol.pairing.PairingOwnerBinding
import app.hermesmobile.protocol.pairing.PairingOwnerView
import app.hermesmobile.protocol.pairing.PairingScope
import app.hermesmobile.protocol.pairing.PairingSessionId
import app.hermesmobile.protocol.pairing.PairingSessionState
import app.hermesmobile.protocol.pairing.RevokePairingDeviceRequest
import app.hermesmobile.protocol.pairing.RevokedPairingDevice
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Rule
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

@OptIn(ExperimentalCoroutinesApi::class)
class DevicePairingViewModelTest {
    @get:Rule
    val dispatcherRule = MainDispatcherRule()

    @Test
    fun `claim sends exact owner binding and clears code from state`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(ownerView()),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel, requestControl = true)

        viewModel.claim()
        advanceUntilIdle()

        val request = assertNotNull(actions.claimRequest)
        assertEquals(PairingCode.fromUserInput("2AB3-C4D5"), request.pairingCode)
        assertEquals(WORKSPACE_ID, request.workspaceId)
        assertEquals(AGENT_ID, request.agentId)
        assertEquals("Office Mac", request.deviceDisplayName)
        assertEquals(
            setOf(PairingScope.SESSION_OBSERVE, PairingScope.SESSION_CONTROL_REQUEST),
            request.scopes,
        )
        assertEquals("", viewModel.state.value.pairingCodeInput)
        assertEquals(DevicePairingPhase.REVIEW, viewModel.state.value.phase)
    }

    @Test
    fun `confirm is ignored until fingerprint was checked and then echoes server review`() = runTest {
        val claimed = ownerView()
        val confirmed = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
        )
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(claimed),
            confirmResult = PairingActionResult.Data(confirmed),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        advanceUntilIdle()

        viewModel.confirm()
        advanceUntilIdle()
        assertEquals(0, actions.confirmCount)

        viewModel.onFingerprintVerificationChanged(true)
        viewModel.confirm()
        runCurrent()

        assertEquals(1, actions.confirmCount)
        assertEquals(claimed.pairingSessionId, actions.confirmSessionId)
        assertSame(claimed, actions.confirmExpectedOwnerView)
        assertEquals(
            ConfirmPairingRequest(
                credentialFingerprint = FINGERPRINT,
                expectedRevision = 2,
            ),
            actions.confirmRequest,
        )
        assertEquals(
            DevicePairingPhase.AWAITING_CONNECTOR_PROOF,
            viewModel.state.value.phase,
        )
        assertFalse(viewModel.state.value.grantsController)
    }

    @Test
    fun `awaiting proof polls after one second updates active revision and stops`() = runTest {
        val awaiting = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
        )
        val active = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.ACTIVE,
            revision = 4,
            deviceRevision = 5,
        )
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(awaiting),
            statusResults = ArrayDeque(listOf(PairingActionResult.Data(active))),
            revokeResult = PairingActionResult.Data(
                RevokedPairingDevice(
                    deviceId = PairingDeviceId(DEVICE_ID),
                    revision = 5,
                    revokedAt = "2026-08-01T00:30:00Z",
                ),
            ),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)

        viewModel.claim()
        runCurrent()
        assertEquals(0, actions.statusCount)
        advanceTimeBy(999)
        runCurrent()
        assertEquals(0, actions.statusCount)
        advanceTimeBy(1)
        runCurrent()

        assertEquals(DevicePairingPhase.ACTIVE, viewModel.state.value.phase)
        assertEquals(4, viewModel.state.value.ownerView?.revision)
        assertEquals(5, viewModel.state.value.ownerView?.deviceRevision)
        advanceTimeBy(5_000)
        runCurrent()
        assertEquals(1, actions.statusCount)

        viewModel.revoke()
        runCurrent()
        assertEquals(5, actions.revokeRequest?.expectedRevision)
        assertEquals(DevicePairingPhase.REVOKED, viewModel.state.value.phase)
    }

    @Test
    fun `terminal and authentication status stop polling`() = runTest {
        val awaiting = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
        )
        val blocked = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.BLOCKED,
            revision = 4,
            deviceRevision = 5,
        )
        val terminalActions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(awaiting),
            statusResults = ArrayDeque(listOf(PairingActionResult.Data(blocked))),
        )
        val terminalViewModel = DevicePairingViewModel(terminalActions)
        enterValidClaim(terminalViewModel)
        terminalViewModel.claim()
        runCurrent()
        advanceTimeBy(1_000)
        runCurrent()

        assertEquals(DevicePairingPhase.BLOCKED, terminalViewModel.state.value.phase)
        advanceTimeBy(5_000)
        runCurrent()
        assertEquals(1, terminalActions.statusCount)

        val authActions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(awaiting),
            statusResults = ArrayDeque(
                listOf(
                    PairingActionResult.Failed(PairingFailure.AuthenticationRequired),
                ),
            ),
        )
        val authViewModel = DevicePairingViewModel(authActions)
        enterValidClaim(authViewModel)
        authViewModel.claim()
        runCurrent()
        advanceTimeBy(1_000)
        runCurrent()

        assertEquals(
            DevicePairingPhase.AUTHENTICATION_REQUIRED,
            authViewModel.state.value.phase,
        )
        advanceTimeBy(5_000)
        runCurrent()
        assertEquals(1, authActions.statusCount)
    }

    @Test
    fun `status retries are paced bounded and honor Retry-After`() = runTest {
        val awaiting = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
        )
        val active = awaiting.copy(
            activationState = PairingActivationState.ACTIVE,
            revision = 4,
        )
        val rateLimitedActions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(awaiting),
            statusResults = ArrayDeque(
                listOf(
                    PairingActionResult.Failed(
                        PairingFailure.Contract(
                            PairingErrorCode.RATE_LIMITED,
                            retryAfterSeconds = 2,
                        ),
                    ),
                    PairingActionResult.Data(active),
                ),
            ),
        )
        val rateLimitedViewModel = DevicePairingViewModel(rateLimitedActions)
        enterValidClaim(rateLimitedViewModel)
        rateLimitedViewModel.claim()
        runCurrent()
        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(1, rateLimitedActions.statusCount)
        advanceTimeBy(1_999)
        runCurrent()
        assertEquals(1, rateLimitedActions.statusCount)
        advanceTimeBy(1)
        runCurrent()
        assertEquals(DevicePairingPhase.ACTIVE, rateLimitedViewModel.state.value.phase)

        val unavailableActions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(awaiting),
            statusResults = ArrayDeque(
                List(3) {
                    PairingActionResult.Failed(PairingFailure.Unavailable)
                },
            ),
        )
        val unavailableViewModel = DevicePairingViewModel(unavailableActions)
        enterValidClaim(unavailableViewModel)
        unavailableViewModel.claim()
        runCurrent()
        repeat(3) {
            advanceTimeBy(1_000)
            runCurrent()
        }
        assertEquals(3, unavailableActions.statusCount)
        assertEquals(DevicePairingPhase.ERROR, unavailableViewModel.state.value.phase)
        advanceTimeBy(5_000)
        runCurrent()
        assertEquals(3, unavailableActions.statusCount)
    }

    @Test
    fun `leaving or resetting cancels poll and stale response cannot overwrite new flow`() =
        runTest {
            val awaiting = ownerView(
                state = PairingSessionState.CONFIRMED,
                activationState = PairingActivationState.AWAITING_PROOF,
                revision = 3,
            )
            val active = awaiting.copy(
                activationState = PairingActivationState.ACTIVE,
                revision = 4,
            )
            val delayed = CompletableDeferred<PairingActionResult<PairingOwnerView>>()
            val actions = FakePairingOwnerActions(
                claimResult = PairingActionResult.Data(awaiting),
                statusHandler = { _, _ -> delayed.await() },
            )
            val viewModel = DevicePairingViewModel(actions)
            enterValidClaim(viewModel)
            viewModel.claim()
            runCurrent()
            advanceTimeBy(1_000)
            runCurrent()
            assertEquals(1, actions.statusCount)

            viewModel.onHidden()
            viewModel.reset()
            delayed.complete(PairingActionResult.Data(active))
            runCurrent()

            assertEquals(DevicePairingPhase.ENTER_CODE, viewModel.state.value.phase)
            assertEquals(null, viewModel.state.value.ownerView)
            advanceTimeBy(5_000)
            runCurrent()
            assertEquals(1, actions.statusCount)
        }

    @Test
    fun `fingerprint mismatch cancels with the security-specific reason`() = runTest {
        val claimed = ownerView()
        val cancelled = ownerView(
            state = PairingSessionState.CANCELLED,
            activationState = PairingActivationState.BLOCKED,
            revision = 3,
        )
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(claimed),
            cancelResult = PairingActionResult.Data(cancelled),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        runCurrent()

        viewModel.rejectFingerprint()
        advanceUntilIdle()

        assertEquals(claimed.pairingSessionId, actions.cancelSessionId)
        assertSame(claimed, actions.cancelExpectedOwnerView)
        assertEquals(
            CancelPairingRequest(
                reason = PairingCancelReason.FINGERPRINT_MISMATCH,
                expectedRevision = 2,
            ),
            actions.cancelRequest,
        )
        assertEquals(DevicePairingPhase.CANCELLED, viewModel.state.value.phase)
    }

    @Test
    fun `revoke targets server device identity and displays terminal state`() = runTest {
        val confirmed = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
            deviceRevision = 4,
        )
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(confirmed),
            revokeResult = PairingActionResult.Data(
                RevokedPairingDevice(
                    deviceId = PairingDeviceId(DEVICE_ID),
                    revision = 5,
                    revokedAt = "2026-08-01T00:30:00Z",
                ),
            ),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        runCurrent()

        viewModel.revoke()
        advanceUntilIdle()

        assertEquals(PairingDeviceId(DEVICE_ID), actions.revokeDeviceId)
        assertEquals(
            RevokePairingDeviceRequest(
                reason = DeviceRevokeReason.USER_REQUESTED,
                expectedRevision = 4,
            ),
            actions.revokeRequest,
        )
        assertEquals(DevicePairingPhase.REVOKED, viewModel.state.value.phase)
        assertEquals("2026-08-01T00:30:00Z", viewModel.state.value.revokedAt)
    }

    @Test
    fun `delivery unknown claim freezes payload and retries the same operation`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Failed(PairingFailure.DeliveryUnknown),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)

        viewModel.claim()
        advanceUntilIdle()

        val originalRequest = assertNotNull(actions.claimRequests.single())
        assertEquals(DevicePairingPhase.DELIVERY_UNKNOWN, viewModel.state.value.phase)
        assertEquals(PairingOperationKind.CLAIM, viewModel.state.value.pendingOperation)
        assertTrue(viewModel.state.value.canRetryPending)
        viewModel.onDeviceDisplayNameChanged("Changed Mac")
        assertEquals("Office Mac", viewModel.state.value.deviceDisplayNameInput)

        actions.claimResult = PairingActionResult.Data(ownerView())
        viewModel.retryPending()
        runCurrent()

        assertEquals(2, actions.claimCount)
        assertSame(originalRequest, actions.claimRequests.last())
        assertEquals(DevicePairingPhase.REVIEW, viewModel.state.value.phase)
    }

    @Test
    fun `delivery unknown confirm retries the exact reviewed confirmation`() = runTest {
        val claimed = ownerView()
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(claimed),
            confirmResult = PairingActionResult.Failed(PairingFailure.DeliveryUnknown),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        runCurrent()
        viewModel.onFingerprintVerificationChanged(true)

        viewModel.confirm()
        advanceUntilIdle()

        val originalRequest = assertNotNull(actions.confirmRequests.single())
        assertEquals(DevicePairingPhase.DELIVERY_UNKNOWN, viewModel.state.value.phase)
        assertEquals(PairingOperationKind.CONFIRM, viewModel.state.value.pendingOperation)
        actions.confirmResult = PairingActionResult.Data(
            ownerView(
                state = PairingSessionState.CONFIRMED,
                activationState = PairingActivationState.AWAITING_PROOF,
                revision = 3,
            ),
        )

        viewModel.retryPending()
        runCurrent()

        assertEquals(2, actions.confirmCount)
        assertSame(originalRequest, actions.confirmRequests.last())
        assertEquals(
            DevicePairingPhase.AWAITING_CONNECTOR_PROOF,
            viewModel.state.value.phase,
        )
    }

    @Test
    fun `delivery unknown cancel retries the exact cancellation`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(ownerView()),
            cancelResult = PairingActionResult.Failed(PairingFailure.DeliveryUnknown),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        runCurrent()

        viewModel.cancel()
        advanceUntilIdle()

        val originalRequest = assertNotNull(actions.cancelRequests.single())
        assertEquals(PairingOperationKind.CANCEL, viewModel.state.value.pendingOperation)
        actions.cancelResult = PairingActionResult.Data(
            ownerView(
                state = PairingSessionState.CANCELLED,
                activationState = PairingActivationState.BLOCKED,
                revision = 3,
            ),
        )

        viewModel.retryPending()
        advanceUntilIdle()

        assertEquals(2, actions.cancelCount)
        assertSame(originalRequest, actions.cancelRequests.last())
        assertEquals(DevicePairingPhase.CANCELLED, viewModel.state.value.phase)
    }

    @Test
    fun `delivery unknown revoke retries the exact device revocation`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(
                ownerView(
                    state = PairingSessionState.CONFIRMED,
                    activationState = PairingActivationState.AWAITING_PROOF,
                    revision = 3,
                    deviceRevision = 4,
                ),
            ),
            revokeResult = PairingActionResult.Failed(PairingFailure.DeliveryUnknown),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        runCurrent()

        viewModel.revoke()
        advanceUntilIdle()

        val originalRequest = assertNotNull(actions.revokeRequests.single())
        assertEquals(PairingOperationKind.REVOKE, viewModel.state.value.pendingOperation)
        actions.revokeResult = PairingActionResult.Data(
            RevokedPairingDevice(
                deviceId = PairingDeviceId(DEVICE_ID),
                revision = 5,
                revokedAt = "2026-08-01T00:30:00Z",
            ),
        )

        viewModel.retryPending()
        advanceUntilIdle()

        assertEquals(2, actions.revokeCount)
        assertSame(originalRequest, actions.revokeRequests.last())
        assertEquals(DevicePairingPhase.REVOKED, viewModel.state.value.phase)
    }

    @Test
    fun `awaiting proof can cancel the confirmed session with its snapshot`() = runTest {
        val awaitingProof = ownerView(
            state = PairingSessionState.CONFIRMED,
            activationState = PairingActivationState.AWAITING_PROOF,
            revision = 3,
        )
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Data(awaitingProof),
            cancelResult = PairingActionResult.Data(
                ownerView(
                    state = PairingSessionState.CANCELLED,
                    activationState = PairingActivationState.BLOCKED,
                    revision = 4,
                ),
            ),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        runCurrent()

        viewModel.cancel()
        advanceUntilIdle()

        assertEquals(1, actions.cancelCount)
        assertEquals(awaitingProof.pairingSessionId, actions.cancelSessionId)
        assertSame(awaitingProof, actions.cancelExpectedOwnerView)
        assertEquals(
            CancelPairingRequest(
                reason = PairingCancelReason.OWNER_CANCELLED,
                expectedRevision = 3,
            ),
            actions.cancelRequest,
        )
        assertEquals(DevicePairingPhase.CANCELLED, viewModel.state.value.phase)
    }

    @Test
    fun `authentication blocking a retry preserves frozen pending operation`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Failed(PairingFailure.DeliveryUnknown),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)
        viewModel.claim()
        advanceUntilIdle()
        val frozenRequest = assertNotNull(actions.claimRequests.single())

        actions.claimResult = PairingActionResult.Failed(
            PairingFailure.AuthenticationRequired,
        )
        viewModel.retryPending()
        advanceUntilIdle()

        assertEquals(
            DevicePairingPhase.AUTHENTICATION_REQUIRED,
            viewModel.state.value.phase,
        )
        assertEquals(PairingOperationKind.CLAIM, viewModel.state.value.pendingOperation)
        assertTrue(viewModel.state.value.canRetryPending)
        viewModel.reset()
        assertEquals(
            DevicePairingPhase.AUTHENTICATION_REQUIRED,
            viewModel.state.value.phase,
        )

        actions.claimResult = PairingActionResult.Data(ownerView())
        viewModel.retryPending()
        advanceUntilIdle()

        assertEquals(3, actions.claimCount)
        assertTrue(actions.claimRequests.all { it === frozenRequest })
        assertEquals(DevicePairingPhase.REVIEW, viewModel.state.value.phase)
    }

    @Test
    fun `first authentication block preserves frozen claim and rejects reset or edits`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Failed(
                PairingFailure.AuthenticationRequired,
            ),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)

        viewModel.claim()
        advanceUntilIdle()

        val frozenRequest = assertNotNull(actions.claimRequests.single())
        assertEquals(
            DevicePairingPhase.AUTHENTICATION_REQUIRED,
            viewModel.state.value.phase,
        )
        assertEquals(PairingOperationKind.CLAIM, viewModel.state.value.pendingOperation)
        assertTrue(viewModel.state.value.canRetryPending)
        viewModel.onDeviceDisplayNameChanged("Different Mac")
        viewModel.reset()
        assertEquals("Office Mac", viewModel.state.value.deviceDisplayNameInput)
        assertEquals(
            DevicePairingPhase.AUTHENTICATION_REQUIRED,
            viewModel.state.value.phase,
        )

        actions.claimResult = PairingActionResult.Data(ownerView())
        viewModel.retryPending()
        advanceUntilIdle()

        assertEquals(2, actions.claimCount)
        assertTrue(actions.claimRequests.all { it === frozenRequest })
        assertEquals(DevicePairingPhase.REVIEW, viewModel.state.value.phase)
    }

    @Test
    fun `first refresh outage preserves frozen claim for retry`() = runTest {
        val actions = FakePairingOwnerActions(
            claimResult = PairingActionResult.Failed(PairingFailure.Unavailable),
        )
        val viewModel = DevicePairingViewModel(actions)
        enterValidClaim(viewModel)

        viewModel.claim()
        advanceUntilIdle()

        val frozenRequest = assertNotNull(actions.claimRequests.single())
        assertEquals(DevicePairingPhase.ERROR, viewModel.state.value.phase)
        assertEquals(PairingOperationKind.CLAIM, viewModel.state.value.pendingOperation)
        assertTrue(viewModel.state.value.canRetryPending)

        actions.claimResult = PairingActionResult.Data(ownerView())
        viewModel.retryPending()
        advanceUntilIdle()

        assertEquals(2, actions.claimCount)
        assertTrue(actions.claimRequests.all { it === frozenRequest })
        assertEquals(DevicePairingPhase.REVIEW, viewModel.state.value.phase)
    }

    private fun enterValidClaim(
        viewModel: DevicePairingViewModel,
        requestControl: Boolean = false,
    ) {
        viewModel.onPairingCodeChanged("2ab3 c4d5")
        viewModel.onWorkspaceIdChanged(WORKSPACE_ID)
        viewModel.onAgentIdChanged(AGENT_ID)
        viewModel.onDeviceDisplayNameChanged("Office Mac")
        viewModel.onRequestControlScopeChanged(requestControl)
    }

    private class FakePairingOwnerActions(
        var claimResult: PairingActionResult<PairingOwnerView> =
            PairingActionResult.Failed(PairingFailure.Unavailable),
        var confirmResult: PairingActionResult<PairingOwnerView> =
            PairingActionResult.Failed(PairingFailure.Unavailable),
        var statusResults: ArrayDeque<PairingActionResult<PairingOwnerView>> = ArrayDeque(),
        var statusHandler: (suspend (
            PairingSessionId,
            PairingOwnerView,
        ) -> PairingActionResult<PairingOwnerView>)? = null,
        var cancelResult: PairingActionResult<PairingOwnerView> =
            PairingActionResult.Failed(PairingFailure.Unavailable),
        var revokeResult: PairingActionResult<RevokedPairingDevice> =
            PairingActionResult.Failed(PairingFailure.Unavailable),
    ) : PairingOwnerActions {
        var claimRequest: ClaimPairingRequest? = null
        var claimCount = 0
        val claimRequests = mutableListOf<ClaimPairingRequest>()
        var confirmCount = 0
        var confirmSessionId: PairingSessionId? = null
        var confirmExpectedOwnerView: PairingOwnerView? = null
        var confirmRequest: ConfirmPairingRequest? = null
        val confirmRequests = mutableListOf<ConfirmPairingRequest>()
        var statusCount = 0
        val statusExpectedOwnerViews = mutableListOf<PairingOwnerView>()
        var cancelCount = 0
        var cancelSessionId: PairingSessionId? = null
        var cancelExpectedOwnerView: PairingOwnerView? = null
        var cancelRequest: CancelPairingRequest? = null
        val cancelRequests = mutableListOf<CancelPairingRequest>()
        var revokeCount = 0
        var revokeDeviceId: PairingDeviceId? = null
        var revokeRequest: RevokePairingDeviceRequest? = null
        val revokeRequests = mutableListOf<RevokePairingDeviceRequest>()

        override suspend fun claim(
            request: ClaimPairingRequest,
        ): PairingActionResult<PairingOwnerView> {
            claimCount += 1
            claimRequest = request
            claimRequests += request
            return claimResult
        }

        override suspend fun confirm(
            sessionId: PairingSessionId,
            expectedOwnerView: PairingOwnerView,
            request: ConfirmPairingRequest,
        ): PairingActionResult<PairingOwnerView> {
            confirmCount += 1
            confirmSessionId = sessionId
            confirmExpectedOwnerView = expectedOwnerView
            confirmRequest = request
            confirmRequests += request
            return confirmResult
        }

        override suspend fun status(
            sessionId: PairingSessionId,
            expectedOwnerView: PairingOwnerView,
        ): PairingActionResult<PairingOwnerView> {
            statusCount += 1
            statusExpectedOwnerViews += expectedOwnerView
            return statusHandler?.invoke(sessionId, expectedOwnerView)
                ?: statusResults.removeFirstOrNull()
                ?: PairingActionResult.Failed(PairingFailure.Unavailable)
        }

        override suspend fun cancel(
            sessionId: PairingSessionId,
            expectedOwnerView: PairingOwnerView,
            request: CancelPairingRequest,
        ): PairingActionResult<PairingOwnerView> {
            cancelCount += 1
            cancelSessionId = sessionId
            cancelExpectedOwnerView = expectedOwnerView
            cancelRequest = request
            cancelRequests += request
            return cancelResult
        }

        override suspend fun revoke(
            deviceId: PairingDeviceId,
            request: RevokePairingDeviceRequest,
        ): PairingActionResult<RevokedPairingDevice> {
            revokeCount += 1
            revokeDeviceId = deviceId
            revokeRequest = request
            revokeRequests += request
            return revokeResult
        }
    }

    private fun ownerView(
        state: PairingSessionState = PairingSessionState.CLAIMED,
        activationState: PairingActivationState =
            PairingActivationState.WAITING_OWNER_CONFIRMATION,
        revision: Int = 2,
        deviceRevision: Int = 4,
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
        deviceRevision = deviceRevision,
    )

    private companion object {
        const val WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
        const val AGENT_ID = "66666666-6666-4666-8666-666666666666"
        const val SESSION_ID = "22222222-2222-4222-8222-222222222222"
        const val DEVICE_ID = "77777777-7777-4777-8777-777777777777"
        const val FINGERPRINT = "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    }
}
