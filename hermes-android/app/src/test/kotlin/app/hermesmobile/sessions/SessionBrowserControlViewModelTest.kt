package app.hermesmobile.sessions

import app.hermesmobile.connection.MainDispatcherRule
import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.MobileControlMethods
import app.hermesmobile.protocol.gateway.PendingInputKind
import app.hermesmobile.protocol.gateway.PendingInputRespondResponse
import app.hermesmobile.protocol.gateway.PromptSubmitResponse
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyAnswer
import app.hermesmobile.protocol.gateway.SessionCommandState
import app.hermesmobile.protocol.gateway.SessionCommandStatus
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControlReleaseResponse
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionControllerResult
import app.hermesmobile.protocol.gateway.SessionInterruptResponse
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.protocol.gateway.SessionSteerResponse
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionPage
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import app.hermesmobile.sessions.control.CommandPhase
import app.hermesmobile.sessions.control.ControlLossReason
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Rule
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

@OptIn(ExperimentalCoroutinesApi::class)
class SessionBrowserControlViewModelTest {
    @get:Rule
    val mainDispatcherRule = MainDispatcherRule()

    @Test
    fun `matching observed runtime acquires controller before enabling composer`() = runTest {
        val fixture = Fixture(this)

        fixture.open()
        fixture.realtime.updates.emit(
            SessionRealtimeUpdate.Projection(
                RealtimeSessionReducer().seed(
                    fixture.transcript,
                    RuntimeSessionId("runtime-1"),
                    connectionEpoch = 1,
                ),
            ),
        )
        runCurrent()

        assertEquals(
            listOf(fixture.session to RuntimeSessionId("runtime-1")),
            fixture.control.openRequests,
        )
        assertIs<ControlMode.Controller>(fixture.viewModel.state.value.control.mode)
        assertEquals(RealtimeControlStatus.CONTROLLER, fixture.viewModel.state.value.controlStatus)
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `unavailable control open exits acquiring and keeps composer fail closed`() = runTest {
        val fixture = Fixture(
            scope = this,
            controlOpenResult = SessionControlOpenResult.Unavailable("control relay unavailable"),
        )

        fixture.open()
        fixture.realtime.updates.emit(
            SessionRealtimeUpdate.Projection(
                RealtimeSessionReducer().seed(
                    fixture.transcript,
                    RuntimeSessionId("runtime-1"),
                    connectionEpoch = 1,
                ),
            ),
        )
        runCurrent()

        val lost = assertIs<ControlMode.Lost>(fixture.viewModel.state.value.control.mode)
        assertEquals(ControlLossReason.CONNECTION_LOST, lost.reason)
        assertTrue(!fixture.viewModel.state.value.canEditComposer)
        assertTrue(!fixture.viewModel.state.value.canSend)
        assertEquals(0, fixture.channel.acquireCalls)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `failed control acquire closes channel and exits acquiring`() = runTest {
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Timeout },
        )

        fixture.openController()

        val lost = assertIs<ControlMode.Lost>(fixture.viewModel.state.value.control.mode)
        assertEquals(ControlLossReason.CONNECTION_LOST, lost.reason)
        assertTrue(fixture.channel.closed)
        assertTrue(!fixture.viewModel.state.value.canEditComposer)
        assertTrue(!fixture.viewModel.state.value.canSend)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `empty advertised methods keep observer live while control is unavailable`() = runTest {
        val fixture = Fixture(
            scope = this,
            availableMethods = emptySet(),
            acquireHandler = { SessionControllerResult.Unsupported },
        )

        fixture.openController()
        fixture.realtime.updates.emit(
            SessionRealtimeUpdate.Connection(
                status = RealtimeConnectionStatus.LIVE,
                controlStatus = RealtimeControlStatus.OBSERVER,
            ),
        )
        runCurrent()

        assertEquals(
            ControlMode.Lost(ControlLossReason.REJECTED),
            fixture.viewModel.state.value.control.mode,
        )
        assertEquals(emptySet(), fixture.viewModel.state.value.controlAvailableMethods)
        assertEquals(
            RealtimeConnectionStatus.LIVE,
            fixture.viewModel.state.value.realtimeConnectionStatus,
        )
        assertTrue(fixture.viewModel.state.value.realtime != null)
        assertTrue(!fixture.viewModel.state.value.canEditComposer)
        assertTrue(!fixture.viewModel.state.value.canSend)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `control snapshot restores clarify draft state and session exit clears it`() = runTest {
        val pending = SessionPendingInput.Clarify(
            requestId = "clarify-1",
            question = "Which target?",
            choices = emptyList(),
            allowOther = true,
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        val fixture = Fixture(
            scope = this,
            acquireHandler = {
                SessionControllerResult.Success(
                    SessionControlLease(
                        leaseId = SessionControlLeaseId("lease-1"),
                        expiresAtEpochMs = 9_000_000_000_000L,
                        controlRevision = 1L,
                        controllerKind = SessionControllerKind.MOBILE,
                        controllerLabel = "Hermes Mobile",
                        pendingInput = pending,
                    ),
                )
            },
        )

        fixture.openController()
        assertEquals("clarify-1", fixture.viewModel.state.value.pendingInteraction.requestId)
        assertTrue(!fixture.viewModel.state.value.canEditComposer)
        assertTrue(!fixture.viewModel.state.value.canSend)

        fixture.viewModel.updatePendingOtherDraft("Deploy staging")
        assertEquals(
            "Deploy staging",
            fixture.viewModel.state.value.pendingInteraction.otherDraft,
        )

        fixture.viewModel.backToSessions()
        runCurrent()
        assertEquals(null, fixture.viewModel.state.value.pendingInteraction.requestId)
        assertEquals("", fixture.viewModel.state.value.pendingInteraction.otherDraft)
    }

    @Test
    fun `pending choice is validated against snapshot and always requires confirmation`() = runTest {
        val pending = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "",
            command = "pwd",
            choices = listOf(
                app.hermesmobile.protocol.gateway.SessionApprovalChoice.ALLOW_ALWAYS,
                app.hermesmobile.protocol.gateway.SessionApprovalChoice.DENY,
            ),
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        val fixture = Fixture(
            scope = this,
            acquireHandler = {
                SessionControllerResult.Success(
                    SessionControlLease(
                        leaseId = SessionControlLeaseId("lease-1"),
                        expiresAtEpochMs = 9_000_000_000_000L,
                        controlRevision = 1L,
                        controllerKind = SessionControllerKind.MOBILE,
                        controllerLabel = "Hermes Mobile",
                        pendingInput = pending,
                    ),
                )
            },
        )

        fixture.openController()
        fixture.viewModel.selectPendingChoice("allow_session")
        assertEquals(null, fixture.viewModel.state.value.pendingInteraction.selectedChoiceId)

        fixture.viewModel.selectPendingChoice("allow_always")
        assertEquals(
            "allow_always",
            fixture.viewModel.state.value.pendingInteraction.selectedChoiceId,
        )
        assertTrue(fixture.viewModel.state.value.pendingInteraction.requiresConfirmation)

        fixture.viewModel.cancelPendingConfirmation()
        assertEquals(null, fixture.viewModel.state.value.pendingInteraction.selectedChoiceId)
        assertTrue(!fixture.viewModel.state.value.pendingInteraction.requiresConfirmation)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `approval duplicate tap is single flight and accepted response advances snapshot`() = runTest {
        val response = CompletableDeferred<SessionControllerResult<PendingInputRespondResponse>>()
        val pending = approvalPending(SessionApprovalChoice.ALLOW_ONCE, SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = { response.await() },
        )
        fixture.openController()
        fixture.viewModel.selectPendingChoice("allow_once")

        fixture.viewModel.submitPendingInput()
        fixture.viewModel.submitPendingInput()
        runCurrent()

        assertEquals(1, fixture.channel.approvalCalls.size)
        assertEquals(ClientRequestId("request-1"), fixture.channel.approvalCalls.single().clientRequestId)
        response.complete(
            SessionControllerResult.Success(
                PendingInputRespondResponse(
                    kind = PendingInputKind.APPROVAL,
                    requestId = "approval-1",
                    clientRequestId = ClientRequestId("request-1"),
                    controlRevision = 2,
                ),
            ),
        )
        runCurrent()

        assertEquals(PendingInputInteractionOutcome.Accepted, fixture.viewModel.state.value.pendingInteraction.outcome)
        assertEquals(null, fixture.viewModel.state.value.pendingInteraction.requestId)
        assertEquals(2L, fixture.viewModel.state.value.control.controlRevision)
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `mismatched pending success preserves exact request tombstone until snapshot reconciliation`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val mismatchedResponses = listOf(
            PendingInputRespondResponse(
                kind = PendingInputKind.APPROVAL,
                requestId = pending.requestId,
                clientRequestId = ClientRequestId("different-client-request"),
                controlRevision = 2,
            ),
            PendingInputRespondResponse(
                kind = PendingInputKind.APPROVAL,
                requestId = "different-pending-request",
                clientRequestId = ClientRequestId("request-1"),
                controlRevision = 2,
            ),
            PendingInputRespondResponse(
                kind = PendingInputKind.CLARIFY,
                requestId = pending.requestId,
                clientRequestId = ClientRequestId("request-1"),
                controlRevision = 2,
            ),
        )

        mismatchedResponses.forEach { response ->
            val fixture = Fixture(
                scope = this,
                acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
                respondApproval = { SessionControllerResult.Success(response) },
                renewHandler = { leaseId ->
                    SessionControllerResult.Success(
                        controlLease(pending, revision = 2, leaseId = leaseId),
                    )
                },
            )
            fixture.openController()
            try {
                fixture.viewModel.selectPendingChoice("deny")
                fixture.viewModel.submitPendingInput()
                runCurrent()

                assertEquals(
                    PendingInputInteractionOutcome.RetryAvailable,
                    fixture.viewModel.state.value.pendingInteraction.outcome,
                )
                assertEquals(pending.requestId, fixture.viewModel.state.value.pendingInteraction.requestId)
                assertEquals(
                    ClientRequestId("request-1"),
                    fixture.viewModel.state.value.pendingInteraction.inFlightClientRequestId,
                )
                assertEquals(1, fixture.channel.renewCalls.size)
            } finally {
                fixture.viewModel.backToSessions()
                runCurrent()
            }
        }
    }

    @Test
    fun `invalid pending response preserves exact request tombstone and refreshes authoritative snapshot`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = { SessionControllerResult.InvalidResponse },
            renewHandler = { leaseId ->
                SessionControllerResult.Success(
                    controlLease(pending, revision = 2, leaseId = leaseId),
                )
            },
        )
        fixture.openController()
        try {
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()

            assertEquals(1, fixture.channel.approvalCalls.size)
            assertEquals(1, fixture.channel.renewCalls.size)
            assertEquals(
                PendingInputInteractionOutcome.RetryAvailable,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
            assertEquals(pending.requestId, fixture.viewModel.state.value.pendingInteraction.requestId)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.pendingInteraction.inFlightClientRequestId,
            )
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `accepted pending response keeps Guide and Queue blocked until the next snapshot arrives`() = runTest {
        val firstPending = approvalPending(SessionApprovalChoice.DENY)
        val nextPending = firstPending.copy(requestId = "approval-2")
        val refreshStarted = CompletableDeferred<Unit>()
        val refreshResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(firstPending)) },
            respondApproval = { call ->
                accepted(call.requestId, call.clientRequestId, PendingInputKind.APPROVAL)
            },
            renewHandler = { leaseId ->
                refreshStarted.complete(Unit)
                refreshResult.await().let { result ->
                    when (result) {
                        is SessionControllerResult.Success -> SessionControllerResult.Success(
                            result.value.copy(leaseId = leaseId),
                        )
                        else -> result
                    }
                }
            },
        )
        fixture.openController()
        fixture.setRunning(true)
        fixture.viewModel.selectPendingChoice("deny")
        fixture.viewModel.submitPendingInput()
        runCurrent()
        assertTrue(refreshStarted.isCompleted)

        try {
            assertTrue(!fixture.viewModel.state.value.canGuide)
            assertTrue(!fixture.viewModel.state.value.canEditComposer)
            assertTrue(!fixture.viewModel.state.value.canSend)

            refreshResult.complete(
                SessionControllerResult.Success(
                    controlLease(nextPending, revision = 3),
                ),
            )
            runCurrent()

            assertEquals("approval-2", fixture.viewModel.state.value.pendingInteraction.requestId)
            assertTrue(!fixture.viewModel.state.value.canGuide)
            assertTrue(!fixture.viewModel.state.value.canEditComposer)
        } finally {
            if (!refreshResult.isCompleted) {
                refreshResult.complete(SessionControllerResult.Timeout)
            }
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `failed pending snapshot refresh revokes control instead of exposing stale authority`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = { call ->
                accepted(call.requestId, call.clientRequestId, PendingInputKind.APPROVAL)
            },
            renewHandler = { SessionControllerResult.Timeout },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()

            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(
                PendingInputInteractionOutcome.Accepted,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
            assertTrue(!fixture.viewModel.state.value.canGuide)
            assertTrue(!fixture.viewModel.state.value.canEditComposer)
            assertTrue(fixture.channel.closed)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `allow always confirmation submits only after confirm and clarify other uses typed answer`() = runTest {
        val approval = approvalPending(SessionApprovalChoice.ALLOW_ALWAYS)
        val approvalFixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(approval)) },
            respondApproval = { call -> accepted(call.requestId, call.clientRequestId, PendingInputKind.APPROVAL) },
        )
        approvalFixture.openController()
        approvalFixture.viewModel.selectPendingChoice("allow_always")
        approvalFixture.viewModel.submitPendingInput()
        runCurrent()
        assertTrue(approvalFixture.channel.approvalCalls.isEmpty())

        approvalFixture.viewModel.confirmPendingChoice()
        runCurrent()
        assertEquals(SessionApprovalChoice.ALLOW_ALWAYS, approvalFixture.channel.approvalCalls.single().choice)
        approvalFixture.viewModel.backToSessions()
        runCurrent()

        val clarify = SessionPendingInput.Clarify(
            requestId = "clarify-1",
            question = "Which target?",
            choices = emptyList(),
            allowOther = true,
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        val clarifyFixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(clarify)) },
            respondClarify = { call -> accepted(call.requestId, call.clientRequestId, PendingInputKind.CLARIFY) },
        )
        clarifyFixture.openController()
        clarifyFixture.viewModel.updatePendingOtherDraft("Deploy staging")
        clarifyFixture.viewModel.submitPendingInput()
        runCurrent()

        assertEquals(
            SessionClarifyAnswer.Other("Deploy staging"),
            clarifyFixture.channel.clarifyCalls.single().answer,
        )
        clarifyFixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `unknown approval delivery retries only after same snapshot and reuses frozen id and payload`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.ALLOW_ONCE, SessionApprovalChoice.DENY)
        var responseCount = 0
        var renewCount = 0
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = { call ->
                responseCount += 1
                if (responseCount == 1) {
                    SessionControllerResult.Timeout
                } else {
                    accepted(call.requestId, call.clientRequestId, PendingInputKind.APPROVAL)
                }
            },
            renewHandler = { leaseId ->
                renewCount += 1
                SessionControllerResult.Success(
                    controlLease(
                        pending = if (renewCount == 1) pending else null,
                        revision = (renewCount + 1).toLong(),
                        leaseId = leaseId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.viewModel.selectPendingChoice("allow_once")
        fixture.viewModel.submitPendingInput()
        runCurrent()

        assertEquals(PendingInputInteractionOutcome.RetryAvailable, fixture.viewModel.state.value.pendingInteraction.outcome)
        fixture.viewModel.selectPendingChoice("deny")
        assertEquals("allow_once", fixture.viewModel.state.value.pendingInteraction.selectedChoiceId)

        fixture.viewModel.submitPendingInput()
        runCurrent()
        assertEquals(2, fixture.channel.approvalCalls.size)
        assertEquals(
            listOf(ClientRequestId("request-1"), ClientRequestId("request-1")),
            fixture.channel.approvalCalls.map(ApprovalCall::clientRequestId),
        )
        assertEquals(
            listOf(SessionApprovalChoice.ALLOW_ONCE, SessionApprovalChoice.ALLOW_ONCE),
            fixture.channel.approvalCalls.map(ApprovalCall::choice),
        )
        assertEquals(PendingInputInteractionOutcome.Accepted, fixture.viewModel.state.value.pendingInteraction.outcome)
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `stale pending response maps 4208 to resolved elsewhere`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            renewHandler = { leaseId ->
                SessionControllerResult.Success(
                    controlLease(
                        pending = null,
                        revision = 2,
                        leaseId = leaseId,
                    ),
                )
            },
            respondApproval = {
                SessionControllerResult.RpcFailure(
                    app.hermesmobile.protocol.gateway.JsonRpcError(
                        code = 4208,
                        message = "must-not-render request details",
                        data = null,
                    ),
                )
            },
        )
        fixture.openController()
        try {
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()

            assertEquals(
                PendingInputInteractionOutcome.ResolvedElsewhere,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
            assertEquals(null, fixture.viewModel.state.value.pendingInteraction.requestId)
            assertEquals(1, fixture.channel.renewCalls.size)
            assertEquals(
                null,
                (fixture.viewModel.state.value.control.mode as ControlMode.Controller).lease.pendingInput,
            )
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `stale pending response freezes mutations until replacement snapshot arrives`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val replacement = pending.copy(requestId = "approval-2")
        val refreshStarted = CompletableDeferred<Unit>()
        val refreshResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            renewHandler = { leaseId ->
                refreshStarted.complete(Unit)
                refreshResult.await().let { result ->
                    when (result) {
                        is SessionControllerResult.Success -> SessionControllerResult.Success(
                            result.value.copy(leaseId = leaseId),
                        )
                        else -> result
                    }
                }
            },
            respondApproval = {
                SessionControllerResult.RpcFailure(
                    app.hermesmobile.protocol.gateway.JsonRpcError(
                        code = 4208,
                        message = "stale",
                        data = null,
                    ),
                )
            },
        )
        try {
            fixture.openController()
            fixture.setRunning(true)
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()
            assertTrue(refreshStarted.isCompleted)

            val refreshing = fixture.viewModel.state.value
            assertTrue(refreshing.isPendingInputSnapshotRefreshing)
            assertTrue(!refreshing.canGuide)
            assertTrue(!refreshing.canEditComposer)
            assertEquals(
                pending.requestId,
                (refreshing.control.mode as ControlMode.Controller).lease.pendingInput?.requestId,
            )

            refreshResult.complete(
                SessionControllerResult.Success(controlLease(replacement, revision = 2)),
            )
            runCurrent()

            assertEquals("approval-2", fixture.viewModel.state.value.pendingInteraction.requestId)
            assertTrue(!fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)
        } finally {
            if (!refreshResult.isCompleted) {
                refreshResult.complete(SessionControllerResult.Timeout)
            }
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `older periodic renewal cannot unfreeze an in flight pending snapshot refresh`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val periodicRenewStarted = CompletableDeferred<Unit>()
        val periodicRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val snapshotRenewStarted = CompletableDeferred<Unit>()
        val snapshotRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        var renewCount = 0
        val fixture = Fixture(
            scope = this,
            clockEpochMs = { 0L },
            leaseRenewLeadMillis = 0L,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(pending).copy(expiresAtEpochMs = 1_000L),
                )
            },
            respondApproval = { SessionControllerResult.Timeout },
            renewHandler = {
                renewCount += 1
                when (renewCount) {
                    1 -> {
                        periodicRenewStarted.complete(Unit)
                        withContext(NonCancellable) { periodicRenewResult.await() }
                    }
                    2 -> {
                        snapshotRenewStarted.complete(Unit)
                        snapshotRenewResult.await()
                    }
                    else -> error("Unexpected renew call $renewCount")
                }
            },
        )
        try {
            fixture.openController()
            advanceTimeBy(1_000L)
            runCurrent()
            assertTrue(periodicRenewStarted.isCompleted)

            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()
            assertTrue(snapshotRenewStarted.isCompleted)
            assertTrue(fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)

            periodicRenewResult.complete(
                SessionControllerResult.Success(
                    controlLease(pending, revision = 1).copy(expiresAtEpochMs = 9_000_000_000_000L),
                ),
            )
            runCurrent()

            assertTrue(fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)
            assertEquals(
                PendingInputInteractionOutcome.DeliveryUnknown,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )

            snapshotRenewResult.complete(
                SessionControllerResult.Success(controlLease(pending, revision = 2)),
            )
            runCurrent()
            assertTrue(!fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)
            assertEquals(
                PendingInputInteractionOutcome.RetryAvailable,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
        } finally {
            if (!periodicRenewResult.isCompleted) {
                periodicRenewResult.complete(SessionControllerResult.Timeout)
            }
            if (!snapshotRenewResult.isCompleted) {
                snapshotRenewResult.complete(SessionControllerResult.Timeout)
            }
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `cancelled periodic renewal cannot overwrite a completed pending snapshot refresh`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val periodicRenewStarted = CompletableDeferred<Unit>()
        val periodicRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val snapshotRenewStarted = CompletableDeferred<Unit>()
        val snapshotRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        var renewCount = 0
        val fixture = Fixture(
            scope = this,
            clockEpochMs = { 0L },
            leaseRenewLeadMillis = 0L,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(pending).copy(expiresAtEpochMs = 1_000L),
                )
            },
            respondApproval = { SessionControllerResult.Timeout },
            renewHandler = {
                renewCount += 1
                when (renewCount) {
                    1 -> {
                        periodicRenewStarted.complete(Unit)
                        withContext(NonCancellable) { periodicRenewResult.await() }
                    }
                    2 -> {
                        snapshotRenewStarted.complete(Unit)
                        snapshotRenewResult.await()
                    }
                    else -> error("Unexpected renew call $renewCount")
                }
            },
        )
        try {
            fixture.openController()
            advanceTimeBy(1_000L)
            runCurrent()
            assertTrue(periodicRenewStarted.isCompleted)

            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()
            assertTrue(snapshotRenewStarted.isCompleted)

            val refreshedLease = controlLease(pending = pending, revision = 2)
            snapshotRenewResult.complete(
                SessionControllerResult.Success(refreshedLease),
            )
            runCurrent()
            assertEquals(
                refreshedLease.leaseId,
                (fixture.viewModel.state.value.control.mode as ControlMode.Controller).lease.leaseId,
            )
            assertTrue(!fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)

            periodicRenewResult.complete(
                SessionControllerResult.Success(
                    controlLease(
                        pending = pending.copy(requestId = "stale-periodic-request"),
                        revision = 3,
                        leaseId = SessionControlLeaseId("lease-stale-periodic"),
                    ),
                ),
            )
            runCurrent()

            val finalState = fixture.viewModel.state.value
            val finalLease = (finalState.control.mode as ControlMode.Controller).lease
            assertEquals(refreshedLease.leaseId, finalLease.leaseId)
            assertEquals(2L, finalLease.controlRevision)
            assertEquals(pending.requestId, finalState.pendingInteraction.requestId)
            assertEquals(
                PendingInputInteractionOutcome.RetryAvailable,
                finalState.pendingInteraction.outcome,
            )
        } finally {
            if (!periodicRenewResult.isCompleted) {
                periodicRenewResult.complete(SessionControllerResult.Timeout)
            }
            if (!snapshotRenewResult.isCompleted) {
                snapshotRenewResult.complete(SessionControllerResult.Timeout)
            }
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `stale dedicated pending snapshot revision revokes control`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val periodicRenewStarted = CompletableDeferred<Unit>()
        val periodicRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val snapshotRenewStarted = CompletableDeferred<Unit>()
        val snapshotRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        var renewCount = 0
        val fixture = Fixture(
            scope = this,
            clockEpochMs = { 0L },
            leaseRenewLeadMillis = 0L,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(pending).copy(expiresAtEpochMs = 1_000L),
                )
            },
            respondApproval = { SessionControllerResult.Timeout },
            renewHandler = {
                renewCount += 1
                when (renewCount) {
                    1 -> {
                        periodicRenewStarted.complete(Unit)
                        withContext(NonCancellable) { periodicRenewResult.await() }
                    }
                    2 -> {
                        snapshotRenewStarted.complete(Unit)
                        snapshotRenewResult.await()
                    }
                    else -> error("Unexpected renew call $renewCount")
                }
            },
        )
        try {
            fixture.openController()
            advanceTimeBy(1_000L)
            runCurrent()
            assertTrue(periodicRenewStarted.isCompleted)

            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()
            assertTrue(snapshotRenewStarted.isCompleted)

            periodicRenewResult.complete(
                SessionControllerResult.Success(
                    controlLease(pending, revision = 3),
                ),
            )
            runCurrent()
            assertEquals(3L, fixture.viewModel.state.value.control.controlRevision)
            assertTrue(fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)

            snapshotRenewResult.complete(
                SessionControllerResult.Success(
                    controlLease(pending, revision = 2),
                ),
            )
            runCurrent()

            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertTrue(fixture.channel.closed)
        } finally {
            if (!periodicRenewResult.isCompleted) {
                periodicRenewResult.complete(SessionControllerResult.Timeout)
            }
            if (!snapshotRenewResult.isCompleted) {
                snapshotRenewResult.complete(SessionControllerResult.Timeout)
            }
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `pending response after lease rollover reconciles through the current lease`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val responseStarted = CompletableDeferred<Unit>()
        val responseResult = CompletableDeferred<SessionControllerResult<PendingInputRespondResponse>>()
        val periodicRenewStarted = CompletableDeferred<Unit>()
        val periodicRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val snapshotRenewStarted = CompletableDeferred<Unit>()
        val snapshotRenewResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        var renewCount = 0
        val fixture = Fixture(
            scope = this,
            clockEpochMs = { 0L },
            leaseRenewLeadMillis = 0L,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(pending).copy(expiresAtEpochMs = 1_000L),
                )
            },
            respondApproval = {
                responseStarted.complete(Unit)
                withContext(NonCancellable) { responseResult.await() }
            },
            renewHandler = {
                renewCount += 1
                when (renewCount) {
                    1 -> {
                        periodicRenewStarted.complete(Unit)
                        periodicRenewResult.await()
                    }
                    2 -> {
                        snapshotRenewStarted.complete(Unit)
                        snapshotRenewResult.await()
                    }
                    else -> error("Unexpected renew call $renewCount")
                }
            },
        )
        try {
            fixture.openController()

            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()
            assertTrue(responseStarted.isCompleted)

            advanceTimeBy(1_000L)
            runCurrent()
            assertTrue(periodicRenewStarted.isCompleted)
            val replacementLeaseId = SessionControlLeaseId("lease-replacement")
            periodicRenewResult.complete(
                SessionControllerResult.Success(
                    controlLease(
                        pending = pending,
                        revision = 2,
                        leaseId = replacementLeaseId,
                    ),
                ),
            )
            runCurrent()

            responseResult.complete(
                accepted(
                    requestId = pending.requestId,
                    clientRequestId = ClientRequestId("request-1"),
                    kind = PendingInputKind.APPROVAL,
                ),
            )
            runCurrent()
            assertTrue(snapshotRenewStarted.isCompleted)
            assertEquals(
                listOf(SessionControlLeaseId("lease-1"), replacementLeaseId),
                fixture.channel.renewCalls,
            )
            assertTrue(fixture.viewModel.state.value.isPendingInputSnapshotRefreshing)
            assertEquals(
                PendingInputInteractionOutcome.DeliveryUnknown,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )

            snapshotRenewResult.complete(
                SessionControllerResult.Success(
                    controlLease(
                        pending = pending,
                        revision = 3,
                        leaseId = replacementLeaseId,
                    ),
                ),
            )
            runCurrent()

            assertEquals(
                PendingInputInteractionOutcome.RetryAvailable,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
            assertEquals(pending.requestId, fixture.viewModel.state.value.pendingInteraction.requestId)
        } finally {
            if (!responseResult.isCompleted) {
                responseResult.complete(SessionControllerResult.Timeout)
            }
            if (!periodicRenewResult.isCompleted) {
                periodicRenewResult.complete(SessionControllerResult.Timeout)
            }
            if (!snapshotRenewResult.isCompleted) {
                snapshotRenewResult.complete(SessionControllerResult.Timeout)
            }
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `pending response conflicts and invalid answers fail without server detail`() = runTest {
        val cases = listOf(
            4207 to "This response could not be reconciled. Refresh the request before trying again.",
            4213 to "The selected response is no longer allowed.",
        )
        cases.forEach { (code, expectedSummary) ->
            val pending = approvalPending(SessionApprovalChoice.DENY)
            val fixture = Fixture(
                scope = this,
                acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
                renewHandler = { leaseId ->
                    SessionControllerResult.Success(
                        controlLease(
                            pending = pending,
                            revision = 2,
                            leaseId = leaseId,
                        ),
                    )
                },
                respondApproval = {
                    SessionControllerResult.RpcFailure(
                        app.hermesmobile.protocol.gateway.JsonRpcError(
                            code = code,
                            message = "must-not-render request_id=private",
                            data = null,
                        ),
                    )
                },
            )
            fixture.openController()
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()

            assertEquals(
                PendingInputInteractionOutcome.Failed(expectedSummary),
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
            assertEquals("approval-1", fixture.viewModel.state.value.pendingInteraction.requestId)
            assertEquals(1, fixture.channel.renewCalls.size)
            assertEquals(
                code == 4207,
                fixture.viewModel.state.value.pendingInteraction.canSubmit,
            )
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unexpected pending response failure never exposes server detail`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = {
                SessionControllerResult.RpcFailure(
                    app.hermesmobile.protocol.gateway.JsonRpcError(
                        code = 4999,
                        message = "private approval payload request_id=secret",
                        data = null,
                    ),
                )
            },
        )
        fixture.openController()
        try {
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()

            assertEquals(
                PendingInputInteractionOutcome.Failed(
                    "Hermes could not accept this response.",
                ),
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `pending response conflict freezes resend until authoritative refresh completes`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val renewalStarted = CompletableDeferred<Unit>()
        val renewalResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = {
                SessionControllerResult.RpcFailure(
                    app.hermesmobile.protocol.gateway.JsonRpcError(
                        code = 4207,
                        message = "must-not-render request_id=private",
                        data = null,
                    ),
                )
            },
            renewHandler = { leaseId ->
                renewalStarted.complete(Unit)
                renewalResult.await()
                SessionControllerResult.Success(
                    controlLease(
                        pending = pending,
                        revision = 2,
                        leaseId = leaseId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.viewModel.selectPendingChoice("deny")
        fixture.viewModel.submitPendingInput()
        runCurrent()

        assertTrue(renewalStarted.isCompleted)
        assertEquals(1, fixture.channel.approvalCalls.size)
        assertEquals(
            ClientRequestId("request-1"),
            fixture.viewModel.state.value.pendingInteraction.inFlightClientRequestId,
        )
        assertEquals(false, fixture.viewModel.state.value.pendingInteraction.canSubmit)

        fixture.viewModel.submitPendingInput()
        runCurrent()
        assertEquals(1, fixture.channel.approvalCalls.size)

        renewalResult.complete(SessionControllerResult.Unsupported)
        runCurrent()
        assertEquals(null, fixture.viewModel.state.value.pendingInteraction.inFlightClientRequestId)
        assertTrue(fixture.viewModel.state.value.pendingInteraction.canSubmit)
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `control reconnect restores draft for the same pending request`() = runTest {
        val pending = SessionPendingInput.Clarify(
            requestId = "clarify-1",
            question = "Which target?",
            choices = emptyList(),
            allowOther = true,
            expiresAtEpochMs = 9_000_000_000_000L,
        )
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
        )
        fixture.openController()
        fixture.viewModel.updatePendingOtherDraft("Preserve me")

        fixture.channel.transportEvents.emit(SessionControlTransportEvent.Closed("network"))
        runCurrent()
        assertEquals("Preserve me", fixture.viewModel.state.value.pendingInteraction.otherDraft)

        fixture.viewModel.retryControlConnection()
        runCurrent()
        assertEquals(2, fixture.control.openRequests.size)
        assertEquals("Preserve me", fixture.viewModel.state.value.pendingInteraction.otherDraft)
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `control loss makes in flight pending response retryable after same snapshot`() = runTest {
        val firstResponse = CompletableDeferred<SessionControllerResult<PendingInputRespondResponse>>()
        val pending = approvalPending(SessionApprovalChoice.ALLOW_ONCE)
        var responseCount = 0
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            respondApproval = { call ->
                responseCount += 1
                if (responseCount == 1) {
                    firstResponse.await()
                } else {
                    accepted(call.requestId, call.clientRequestId, PendingInputKind.APPROVAL)
                }
            },
        )
        fixture.openController()
        fixture.viewModel.selectPendingChoice("allow_once")
        fixture.viewModel.submitPendingInput()
        runCurrent()
        assertEquals(1, fixture.channel.approvalCalls.size)

        fixture.channel.transportEvents.emit(SessionControlTransportEvent.Closed("network"))
        runCurrent()
        assertEquals(
            PendingInputInteractionOutcome.DeliveryUnknown,
            fixture.viewModel.state.value.pendingInteraction.outcome,
        )

        fixture.viewModel.retryControlConnection()
        runCurrent()
        assertEquals(
            PendingInputInteractionOutcome.RetryAvailable,
            fixture.viewModel.state.value.pendingInteraction.outcome,
        )
        fixture.viewModel.submitPendingInput()
        runCurrent()
        assertEquals(
            listOf(ClientRequestId("request-1"), ClientRequestId("request-1")),
            fixture.channel.approvalCalls.map(ApprovalCall::clientRequestId),
        )
        assertEquals(
            PendingInputInteractionOutcome.Accepted,
            fixture.viewModel.state.value.pendingInteraction.outcome,
        )
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `control open is single flight while websocket handshake is pending`() = runTest {
        val openGate = CompletableDeferred<Unit>()
        val fixture = Fixture(scope = this, controlOpenGate = openGate)
        fixture.open()
        val projection = RealtimeSessionReducer().seed(
            fixture.transcript,
            RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        )

        fixture.realtime.updates.emit(SessionRealtimeUpdate.Projection(projection))
        runCurrent()
        fixture.realtime.updates.emit(SessionRealtimeUpdate.Projection(projection.copy(running = true)))
        runCurrent()

        assertEquals(1, fixture.control.openRequests.size)

        openGate.complete(Unit)
        runCurrent()
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `late control open from obsolete runtime closes without acquiring`() = runTest {
        val openGate = CompletableDeferred<Unit>()
        val fixture = Fixture(
            scope = this,
            controlOpenGate = openGate,
            controlOpenIgnoresCancellation = true,
        )
        fixture.open()
        fixture.emitProjection(RuntimeSessionId("runtime-1"), running = false)
        fixture.emitRuntime(RuntimeSessionId("runtime-2"))

        openGate.complete(Unit)
        runCurrent()

        try {
            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(1, fixture.control.openRequests.size)
            assertEquals(0, fixture.channel.acquireCalls)
            assertTrue(fixture.channel.closed)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `late acquire from obsolete runtime releases lease without restoring control`() = runTest {
        val acquireStarted = CompletableDeferred<Unit>()
        val acquireResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val fixture = Fixture(
            scope = this,
            acquireHandler = {
                acquireStarted.complete(Unit)
                withContext(NonCancellable) { acquireResult.await() }
            },
        )
        fixture.open()
        fixture.emitProjection(RuntimeSessionId("runtime-1"), running = false)
        assertTrue(acquireStarted.isCompleted)

        fixture.emitRuntime(RuntimeSessionId("runtime-2"))
        acquireResult.complete(
            SessionControllerResult.Success(
                SessionControlLease(
                    leaseId = SessionControlLeaseId("late-lease"),
                    expiresAtEpochMs = 120_000L,
                    controlRevision = 1L,
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = "Hermes Mobile",
                    pendingInput = null,
                ),
            ),
        )
        runCurrent()

        try {
            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(listOf(SessionControlLeaseId("late-lease")), fixture.channel.releaseCalls)
            assertTrue(fixture.channel.closed)
            assertTrue(!fixture.viewModel.state.value.canSend)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `correlated prompt ack does not clear a draft edited while send was pending`() = runTest {
        val pendingSubmit = CompletableDeferred<SessionControllerResult<PromptSubmitResponse>>()
        val fixture = Fixture(scope = this, submit = { pendingSubmit.await() })
        fixture.openController()

        fixture.viewModel.onDraftChanged("first prompt")
        fixture.viewModel.sendPrompt()
        runCurrent()
        assertEquals("first prompt", fixture.viewModel.state.value.composer.draft)

        fixture.viewModel.onDraftChanged("new draft")
        pendingSubmit.complete(
            SessionControllerResult.Success(
                PromptSubmitResponse(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = ClientRequestId("request-1"),
                    clientTurnId = ClientTurnId("turn-1"),
                ),
            ),
        )
        runCurrent()

        assertEquals("new draft", fixture.viewModel.state.value.composer.draft)
        assertEquals(
            CommandPhase.ACCEPTED,
            fixture.viewModel.state.value.commands.commands
                .getValue(ClientRequestId("request-1"))
                .phase,
        )
        assertEquals(1, fixture.channel.submitCalls.size)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `running prompt submission uses authoritative queue acknowledgement and keeps a safe preview`() = runTest {
        val fixture = Fixture(
            scope = this,
            submit = { call ->
                SessionControllerResult.Success(
                    PromptSubmitResponse(
                        status = SessionCommandState.QUEUED,
                        clientRequestId = call.requestId,
                        clientTurnId = call.clientTurnId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onDraftChanged("Run the accessibility checks next")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals(1, fixture.channel.submitCalls.size)
            val command = fixture.viewModel.state.value.commands.commands
                .getValue(ClientRequestId("request-1"))
            assertEquals(CommandPhase.QUEUED, command.phase)
            assertEquals("Run the accessibility checks next", command.promptPreview)
            assertEquals("", fixture.viewModel.state.value.composer.draft)
            assertTrue(fixture.viewModel.state.value.canStop)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `running guidance steers the current turn without consuming the prompt draft`() = runTest {
        val fixture = Fixture(scope = this)
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onDraftChanged("Queue this as the next user turn")
            fixture.viewModel.onGuidanceDraftChanged("Also verify the authorization path")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(
                listOf(
                    SteerCall(
                        leaseId = SessionControlLeaseId("lease-1"),
                        requestId = ClientRequestId("request-1"),
                        text = "Also verify the authorization path",
                    ),
                ),
                fixture.channel.steerCalls,
            )
            assertEquals(
                "Queue this as the next user turn",
                fixture.viewModel.state.value.composer.draft,
            )
            assertEquals("", fixture.viewModel.state.value.guidance.draft)
            assertEquals(SessionGuidancePhase.ACCEPTED, fixture.viewModel.state.value.guidance.phase)
            assertTrue(fixture.viewModel.state.value.canGuide)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unknown guidance delivery reconciles the original request without steering twice`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { SessionControllerResult.Timeout },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = requestId,
                        clientTurnId = null,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Use the original request identity")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(1, fixture.channel.steerCalls.size)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
            assertEquals(SessionGuidancePhase.ACCEPTED, fixture.viewModel.state.value.guidance.phase)
            assertEquals("", fixture.viewModel.state.value.guidance.draft)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `direct unknown guidance response preserves tombstone and reconciles the same request`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { call ->
                SessionControllerResult.Success(
                    SessionSteerResponse(
                        status = SessionCommandState.UNKNOWN,
                        clientRequestId = call.requestId,
                    ),
                )
            },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.UNKNOWN,
                        clientRequestId = requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Keep the exact guidance request")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(
                "Keep the exact guidance request",
                fixture.viewModel.state.value.guidance.submittedText,
            )
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)

            fixture.viewModel.submitGuidance()
            runCurrent()
            assertEquals(1, fixture.channel.steerCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `invalid guidance response preserves tombstone and reconciles only the original request`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { SessionControllerResult.InvalidResponse },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.UNKNOWN,
                        clientRequestId = requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Keep invalid-response guidance exact")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(
                "Keep invalid-response guidance exact",
                fixture.viewModel.state.value.guidance.submittedText,
            )
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)

            fixture.viewModel.submitGuidance()
            runCurrent()
            assertEquals(1, fixture.channel.steerCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `mismatched direct guidance response reconciles only the original request`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = {
                SessionControllerResult.Success(
                    SessionSteerResponse(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = ClientRequestId("different-request"),
                    ),
                )
            },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.UNKNOWN,
                        clientRequestId = requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Do not release this request identity")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)
            assertTrue(!fixture.viewModel.state.value.canGuide)
            assertEquals(1, fixture.channel.steerCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unknown guidance command status preserves the original request tombstone`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { SessionControllerResult.Timeout },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.UNKNOWN,
                        clientRequestId = requestId,
                        clientTurnId = null,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Keep this ambiguous guidance")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(
                "Keep this ambiguous guidance",
                fixture.viewModel.state.value.guidance.submittedText,
            )
            assertEquals("Keep this ambiguous guidance", fixture.viewModel.state.value.guidance.draft)
            assertTrue(!fixture.viewModel.state.value.canGuide)

            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(1, fixture.channel.steerCalls.size)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `mismatched guidance command status preserves the original request tombstone`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { SessionControllerResult.Timeout },
            statusHandler = {
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = ClientRequestId("different-request"),
                        clientTurnId = null,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Keep the request-bound guidance")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(
                "Keep the request-bound guidance",
                fixture.viewModel.state.value.guidance.submittedText,
            )
            assertTrue(!fixture.viewModel.state.value.canGuide)
            assertEquals(1, fixture.channel.steerCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `late guidance result from a rotated lease cannot mutate the new authority`() = runTest {
        val steerStarted = CompletableDeferred<Unit>()
        val steerResult = CompletableDeferred<SessionControllerResult<SessionSteerResponse>>()
        val fixture = Fixture(
            scope = this,
            clockEpochMs = { 0L },
            leaseRenewLeadMillis = 0L,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(null).copy(expiresAtEpochMs = 1_000L),
                )
            },
            renewHandler = { leaseId ->
                assertEquals(SessionControlLeaseId("lease-1"), leaseId)
                SessionControllerResult.Success(
                    controlLease(
                        pending = null,
                        revision = 2,
                        leaseId = SessionControlLeaseId("lease-2"),
                    ),
                )
            },
            steer = { call ->
                steerStarted.complete(Unit)
                withContext(NonCancellable) { steerResult.await() }.also {
                    assertEquals(SessionControlLeaseId("lease-1"), call.leaseId)
                }
            },
        )
        fixture.openController()
        fixture.setRunning(true)
        fixture.viewModel.onGuidanceDraftChanged("Bind this steer to the original lease")
        fixture.viewModel.submitGuidance()
        runCurrent()
        assertTrue(steerStarted.isCompleted)

        advanceTimeBy(1_000L)
        runCurrent()
        assertEquals(
            SessionControlLeaseId("lease-2"),
            (fixture.viewModel.state.value.control.mode as ControlMode.Controller).lease.leaseId,
        )

        steerResult.complete(
            SessionControllerResult.Success(
                SessionSteerResponse(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = ClientRequestId("request-1"),
                ),
            ),
        )
        runCurrent()

        try {
            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(
                "Bind this steer to the original lease",
                fixture.viewModel.state.value.guidance.submittedText,
            )
            assertTrue(!fixture.viewModel.state.value.canGuide)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `late guidance status from a rotated lease cannot mutate the new authority`() = runTest {
        val statusStarted = CompletableDeferred<Unit>()
        val statusResult = CompletableDeferred<SessionControllerResult<SessionCommandStatus>>()
        val fixture = Fixture(
            scope = this,
            clockEpochMs = { 0L },
            leaseRenewLeadMillis = 0L,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(null).copy(expiresAtEpochMs = 1_000L),
                )
            },
            renewHandler = {
                SessionControllerResult.Success(
                    controlLease(
                        pending = null,
                        revision = 2,
                        leaseId = SessionControlLeaseId("lease-2"),
                    ),
                )
            },
            steer = { SessionControllerResult.Timeout },
            statusHandler = {
                statusStarted.complete(Unit)
                withContext(NonCancellable) { statusResult.await() }
            },
        )
        fixture.openController()
        fixture.setRunning(true)
        fixture.viewModel.onGuidanceDraftChanged("Keep status bound to lease one")
        fixture.viewModel.submitGuidance()
        runCurrent()
        assertTrue(statusStarted.isCompleted)

        advanceTimeBy(1_000L)
        runCurrent()
        assertEquals(
            SessionControlLeaseId("lease-2"),
            (fixture.viewModel.state.value.control.mode as ControlMode.Controller).lease.leaseId,
        )

        statusResult.complete(
            SessionControllerResult.Success(
                SessionCommandStatus(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = ClientRequestId("request-1"),
                ),
            ),
        )
        runCurrent()

        try {
            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.guidance.inFlightRequestId,
            )
            assertEquals(1, fixture.channel.steerCalls.size)
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `disconnected guidance preserves its identity until control reacquires and reconciles`() = runTest {
        val steerStarted = CompletableDeferred<Unit>()
        val steerResult = CompletableDeferred<SessionControllerResult<SessionSteerResponse>>()
        val fixture = Fixture(
            scope = this,
            steer = {
                steerStarted.complete(Unit)
                withContext(NonCancellable) { steerResult.await() }
            },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        fixture.viewModel.onGuidanceDraftChanged("Preserve this ambiguous guidance")
        fixture.viewModel.submitGuidance()
        runCurrent()
        assertTrue(steerStarted.isCompleted)

        fixture.channel.transportEvents.emit(
            SessionControlTransportEvent.Closed("network lost"),
        )
        runCurrent()
        steerResult.complete(SessionControllerResult.Disconnected)
        runCurrent()

        assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
        assertEquals(
            ClientRequestId("request-1"),
            fixture.viewModel.state.value.guidance.inFlightRequestId,
        )
        assertEquals(
            "Preserve this ambiguous guidance",
            fixture.viewModel.state.value.guidance.draft,
        )
        assertTrue(!fixture.viewModel.state.value.canGuide)
        assertTrue(fixture.channel.commandStatusCalls.isEmpty())

        fixture.viewModel.retryControlConnection()
        runCurrent()

        try {
            assertEquals(2, fixture.channel.acquireCalls)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
            assertEquals(1, fixture.channel.steerCalls.size)
            assertEquals(SessionGuidancePhase.ACCEPTED, fixture.viewModel.state.value.guidance.phase)
            assertEquals("", fixture.viewModel.state.value.guidance.draft)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `guidance disconnected result revokes control before explicit reconciliation retry`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { SessionControllerResult.Disconnected },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        fixture.viewModel.onGuidanceDraftChanged("Reconcile after reconnect")
        fixture.viewModel.submitGuidance()
        runCurrent()

        try {
            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
            assertTrue(fixture.channel.commandStatusCalls.isEmpty())

            fixture.viewModel.retryControlConnection()
            runCurrent()

            assertEquals(2, fixture.channel.acquireCalls)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
            assertEquals(1, fixture.channel.steerCalls.size)
            assertEquals(SessionGuidancePhase.ACCEPTED, fixture.viewModel.state.value.guidance.phase)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `late guidance reconciliation cannot mutate the next turn with a reused request id`() = runTest {
        val firstStatus = CompletableDeferred<SessionControllerResult<SessionCommandStatus>>()
        val secondStatus = CompletableDeferred<SessionControllerResult<SessionCommandStatus>>()
        var statusCallCount = 0
        val fixture = Fixture(
            scope = this,
            steer = { SessionControllerResult.Timeout },
            statusHandler = {
                withContext(NonCancellable) {
                    if (statusCallCount++ == 0) firstStatus.await() else secondStatus.await()
                }
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        fixture.viewModel.onGuidanceDraftChanged("Only for the first turn")
        fixture.viewModel.submitGuidance()
        runCurrent()
        assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)

        fixture.setRunning(false)
        fixture.setRunning(true)
        fixture.viewModel.onGuidanceDraftChanged("Only for the next turn")
        fixture.viewModel.submitGuidance()
        runCurrent()
        assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)

        firstStatus.complete(
            SessionControllerResult.Success(
                SessionCommandStatus(
                    status = SessionCommandState.REJECTED,
                    clientRequestId = ClientRequestId("request-1"),
                ),
            ),
        )
        runCurrent()

        try {
            assertEquals("Only for the next turn", fixture.viewModel.state.value.guidance.draft)
            assertEquals(SessionGuidancePhase.DELIVERY_UNKNOWN, fixture.viewModel.state.value.guidance.phase)
        } finally {
            secondStatus.complete(
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = ClientRequestId("request-1"),
                    ),
                ),
            )
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `rejected guidance preserves its independent draft`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { call ->
                SessionControllerResult.Success(
                    SessionSteerResponse(
                        status = SessionCommandState.REJECTED,
                        clientRequestId = call.requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Keep this guidance")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals("Keep this guidance", fixture.viewModel.state.value.guidance.draft)
            assertEquals(SessionGuidancePhase.FAILED, fixture.viewModel.state.value.guidance.phase)
            assertTrue(fixture.viewModel.state.value.canGuide)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `authoritative turn completion clears guidance state`() = runTest {
        val fixture = Fixture(scope = this)
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("Only for this execution")
            assertEquals("Only for this execution", fixture.viewModel.state.value.guidance.draft)

            fixture.setRunning(false)

            assertEquals(SessionGuidanceState(), fixture.viewModel.state.value.guidance)
            assertTrue(!fixture.viewModel.state.value.canGuide)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `rejected prompt preserves the draft and becomes retryable`() = runTest {
        val fixture = Fixture(
            scope = this,
            submit = { call ->
                SessionControllerResult.Success(
                    PromptSubmitResponse(
                        status = SessionCommandState.REJECTED,
                        clientRequestId = call.requestId,
                        clientTurnId = call.clientTurnId,
                    ),
                )
            },
        )
        fixture.openController()

        try {
            fixture.viewModel.onDraftChanged("keep this prompt")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals("keep this prompt", fixture.viewModel.state.value.composer.draft)
            assertEquals(null, fixture.viewModel.state.value.composer.submitted)
            assertEquals(
                CommandPhase.REJECTED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            assertTrue(fixture.viewModel.state.value.canSend)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `definitive prompt failure preserves draft and becomes retryable`() = runTest {
        val fixture = Fixture(
            scope = this,
            submit = {
                SessionControllerResult.RpcFailure(
                    app.hermesmobile.protocol.gateway.JsonRpcError(
                        code = 4203,
                        message = "access_token=must-not-render ${"x".repeat(700)}",
                        data = null,
                    ),
                )
            },
        )
        fixture.openController()

        try {
            fixture.viewModel.onDraftChanged("retry this prompt")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals("retry this prompt", fixture.viewModel.state.value.composer.draft)
            assertEquals(null, fixture.viewModel.state.value.composer.submitted)
            assertEquals(
                CommandPhase.FAILED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            val failureSummary = fixture.viewModel.state.value.commands.commands
                .getValue(ClientRequestId("request-1"))
                .failureSummary.orEmpty()
            assertTrue("must-not-render" !in failureSummary)
            assertTrue(failureSummary.codePointCount(0, failureSummary.length) <= 512)
            assertTrue(fixture.viewModel.state.value.canSend)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unknown prompt delivery reconciles original request id without resubmitting`() = runTest {
        val fixture = Fixture(
            this,
            submit = { SessionControllerResult.Timeout },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = requestId,
                        clientTurnId = ClientTurnId("turn-1"),
                    ),
                )
            },
        )
        fixture.openController()

        fixture.viewModel.onDraftChanged("reconcile me")
        fixture.viewModel.sendPrompt()
        runCurrent()

        assertEquals(1, fixture.channel.submitCalls.size)
        assertEquals(
            listOf(ClientRequestId("request-1")),
            fixture.channel.commandStatusCalls,
        )
        assertEquals(
            CommandPhase.ACCEPTED,
            fixture.viewModel.state.value.commands.commands
                .getValue(ClientRequestId("request-1"))
                .phase,
        )
        assertEquals("", fixture.viewModel.state.value.composer.draft)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `unknown prompt ignores command status for a different request`() = runTest {
        val fixture = Fixture(
            this,
            submit = { SessionControllerResult.Timeout },
            statusHandler = {
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = ClientRequestId("different-request"),
                        clientTurnId = ClientTurnId("different-turn"),
                    ),
                )
            },
        )
        fixture.openController()

        try {
            fixture.viewModel.onDraftChanged("keep this submission pending")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals(
                setOf(ClientRequestId("request-1")),
                fixture.viewModel.state.value.commands.commands.keys,
            )
            assertEquals(
                CommandPhase.UNKNOWN,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.composer.submitted?.requestId,
            )
            assertEquals("keep this submission pending", fixture.viewModel.state.value.composer.draft)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `malformed prompt response after commit remains effect unknown until reconciled`() = runTest {
        val pendingStatus = CompletableDeferred<SessionControllerResult<SessionCommandStatus>>()
        val fixture = Fixture(
            this,
            submit = { SessionControllerResult.InvalidResponse },
            statusHandler = { pendingStatus.await() },
        )
        fixture.openController()

        try {
            fixture.viewModel.onDraftChanged("do not submit twice")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals(1, fixture.channel.submitCalls.size)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
            assertEquals(
                CommandPhase.UNKNOWN,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            assertEquals("do not submit twice", fixture.viewModel.state.value.composer.draft)
            assertEquals(
                ClientRequestId("request-1"),
                fixture.viewModel.state.value.composer.submitted?.requestId,
            )
            assertTrue(!fixture.viewModel.state.value.canSend)

            pendingStatus.complete(
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = ClientRequestId("request-1"),
                        clientTurnId = ClientTurnId("turn-1"),
                    ),
                ),
            )
            runCurrent()

            assertEquals(1, fixture.channel.submitCalls.size)
            assertEquals("", fixture.viewModel.state.value.composer.draft)
            assertEquals(null, fixture.viewModel.state.value.composer.submitted)
            assertEquals(
                CommandPhase.ACCEPTED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
        } finally {
            pendingStatus.complete(SessionControllerResult.InvalidResponse)
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `authentication loss while loading older messages releases controller`() = runTest {
        val fixture = Fixture(
            scope = this,
            initialTranscriptOffset = 20,
            authenticateAfterInitialTranscript = true,
        )
        fixture.openController()
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.viewModel.loadOlderMessages()
        runCurrent()

        try {
            assertEquals(
                SessionBrowserPhase.AUTHENTICATION_REQUIRED,
                fixture.viewModel.state.value.phase,
            )
            assertEquals(listOf(SessionControlLeaseId("lease-1")), fixture.channel.releaseCalls)
            assertTrue(fixture.channel.closed)
            assertTrue(!fixture.viewModel.state.value.canSend)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `leaving transcript releases lease closes control and disables composer`() = runTest {
        val fixture = Fixture(this)
        fixture.openController()
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.viewModel.backToSessions()
        runCurrent()

        assertEquals(
            listOf(SessionControlLeaseId("lease-1")),
            fixture.channel.releaseCalls,
        )
        assertTrue(fixture.channel.closed)
        assertTrue(!fixture.viewModel.state.value.canSend)
        assertEquals(RealtimeControlStatus.SERVER_UPGRADE_REQUIRED, fixture.viewModel.state.value.controlStatus)
    }

    @Test
    fun `control transport loss immediately revokes mutation rights`() = runTest {
        val fixture = Fixture(this)
        fixture.openController()
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.channel.transportEvents.emit(
            SessionControlTransportEvent.Closed("network lost"),
        )
        runCurrent()

        try {
            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertTrue(!fixture.viewModel.state.value.canSend)
            assertEquals(RealtimeControlStatus.OBSERVER, fixture.viewModel.state.value.controlStatus)

            fixture.viewModel.start()
            runCurrent()
            fixture.emitProjection(RuntimeSessionId("runtime-1"), running = false)
            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(1, fixture.control.openRequests.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `runtime rollover revokes and releases the old controller without reacquiring`() = runTest {
        val fixture = Fixture(this)
        fixture.openController()
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.emitRuntime(RuntimeSessionId("runtime-2"))
        runCurrent()

        try {
            assertTrue(!fixture.viewModel.state.value.canSend)
            assertEquals(
                ControlMode.Lost(ControlLossReason.CONNECTION_LOST),
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(listOf(SessionControlLeaseId("lease-1")), fixture.channel.releaseCalls)
            assertTrue(fixture.channel.closed)
            assertEquals(1, fixture.control.openRequests.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `controller lease renews before expiry and advances authoritative revision`() = runTest {
        val fixture = Fixture(
            scope = this,
            leaseExpiresAtEpochMs = 2_000L,
            clockEpochMs = { testScheduler.currentTime },
            leaseRenewLeadMillis = 1_000L,
        )
        fixture.openController()

        advanceTimeBy(1_000L)
        runCurrent()

        assertEquals(
            listOf(SessionControlLeaseId("lease-1")),
            fixture.channel.renewCalls,
        )
        assertEquals(2L, fixture.viewModel.state.value.control.controlRevision)
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `lease renewal failure marks an in flight pending response delivery unknown`() = runTest {
        val pendingResponse = CompletableDeferred<SessionControllerResult<PendingInputRespondResponse>>()
        val fixture = Fixture(
            scope = this,
            acquireHandler = {
                SessionControllerResult.Success(
                    controlLease(approvalPending(SessionApprovalChoice.DENY)).copy(
                        expiresAtEpochMs = 2_000L,
                    ),
                )
            },
            respondApproval = { pendingResponse.await() },
            leaseExpiresAtEpochMs = 2_000L,
            clockEpochMs = { testScheduler.currentTime },
            leaseRenewLeadMillis = 1_000L,
            renewHandler = { SessionControllerResult.Timeout },
        )
        fixture.openController()
        fixture.viewModel.selectPendingChoice("deny")
        fixture.viewModel.submitPendingInput()
        runCurrent()
        assertEquals(1, fixture.channel.approvalCalls.size)
        assertEquals(
            ClientRequestId("request-1"),
            fixture.viewModel.state.value.pendingInteraction.inFlightClientRequestId,
        )

        advanceTimeBy(1_000L)
        runCurrent()

        assertEquals(
            PendingInputInteractionOutcome.DeliveryUnknown,
            fixture.viewModel.state.value.pendingInteraction.outcome,
        )
        assertEquals(
            ClientRequestId("request-1"),
            fixture.viewModel.state.value.pendingInteraction.inFlightClientRequestId,
        )
        assertEquals(
            ControlMode.Lost(ControlLossReason.LEASE_EXPIRED),
            fixture.viewModel.state.value.control.mode,
        )
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `late renewal success after leaving transcript cannot restore controller`() = runTest {
        val renewalStarted = CompletableDeferred<Unit>()
        val renewalResult = CompletableDeferred<SessionControllerResult<SessionControlLease>>()
        val fixture = Fixture(
            scope = this,
            leaseExpiresAtEpochMs = 2_000L,
            clockEpochMs = { testScheduler.currentTime },
            leaseRenewLeadMillis = 1_000L,
            renewHandler = {
                renewalStarted.complete(Unit)
                withContext(NonCancellable) { renewalResult.await() }
            },
        )
        fixture.openController()

        advanceTimeBy(1_000L)
        runCurrent()
        assertTrue(renewalStarted.isCompleted)

        fixture.viewModel.backToSessions()
        runCurrent()
        renewalResult.complete(
            SessionControllerResult.Success(
                SessionControlLease(
                    leaseId = SessionControlLeaseId("lease-1"),
                    expiresAtEpochMs = 4_000L,
                    controlRevision = 2L,
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = "Hermes Mobile",
                    pendingInput = null,
                ),
            ),
        )
        runCurrent()

        assertEquals(SessionBrowserPhase.LIST, fixture.viewModel.state.value.phase)
        assertEquals(ControlMode.Disconnected, fixture.viewModel.state.value.control.mode)
        assertTrue(!fixture.viewModel.state.value.canSend)
    }

    @Test
    fun `stop interrupts the active turn once and remains pending until realtime becomes idle`() = runTest {
        val pendingInterrupt = CompletableDeferred<SessionControllerResult<SessionInterruptResponse>>()
        val fixture = Fixture(
            scope = this,
            interrupt = { pendingInterrupt.await() },
        )
        fixture.openController()
        fixture.setRunning(true)
        assertTrue(fixture.viewModel.state.value.canStop)

        fixture.viewModel.stopCurrentTurn()
        runCurrent()

        assertEquals(
            listOf(InterruptCall(SessionControlLeaseId("lease-1"), ClientRequestId("request-1"))),
            fixture.channel.interruptCalls,
        )
        assertTrue(fixture.viewModel.state.value.isInterrupting)
        assertTrue(!fixture.viewModel.state.value.canStop)

        pendingInterrupt.complete(
            SessionControllerResult.Success(
                SessionInterruptResponse(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = ClientRequestId("request-1"),
                ),
            ),
        )
        runCurrent()
        assertTrue(fixture.viewModel.state.value.isInterrupting)

        fixture.setRunning(false)
        assertTrue(!fixture.viewModel.state.value.isInterrupting)

        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `leaving transcript cancels a pending stop and clears its state`() = runTest {
        val pendingInterrupt = CompletableDeferred<SessionControllerResult<SessionInterruptResponse>>()
        val fixture = Fixture(scope = this, interrupt = { pendingInterrupt.await() })
        fixture.openController()
        fixture.setRunning(true)

        fixture.viewModel.stopCurrentTurn()
        runCurrent()
        assertTrue(fixture.viewModel.state.value.isInterrupting)

        fixture.viewModel.backToSessions()
        runCurrent()

        assertTrue(!fixture.viewModel.state.value.isInterrupting)
        assertTrue(fixture.channel.closed)
    }

    @Test
    fun `rejected stop clears pending state while the turn is still running`() = runTest {
        val fixture = Fixture(
            scope = this,
            interrupt = { call ->
                SessionControllerResult.Success(
                    SessionInterruptResponse(
                        status = SessionCommandState.REJECTED,
                        clientRequestId = call.requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.stopCurrentTurn()
            runCurrent()

            assertTrue(!fixture.viewModel.state.value.isInterrupting)
            assertTrue(fixture.viewModel.state.value.canStop)
            assertEquals(
                CommandPhase.REJECTED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `definitive stop failure clears pending state and records failure`() = runTest {
        val fixture = Fixture(
            scope = this,
            interrupt = {
                SessionControllerResult.RpcFailure(
                    app.hermesmobile.protocol.gateway.JsonRpcError(
                        code = 4203,
                        message = "lease_id=must-not-render ${"x".repeat(700)}",
                        data = null,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.stopCurrentTurn()
            runCurrent()

            assertTrue(!fixture.viewModel.state.value.isInterrupting)
            assertTrue(fixture.viewModel.state.value.canStop)
            assertEquals(
                CommandPhase.FAILED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            val failureSummary = fixture.viewModel.state.value.commands.commands
                .getValue(ClientRequestId("request-1"))
                .failureSummary.orEmpty()
            assertTrue("must-not-render" !in failureSummary)
            assertTrue(failureSummary.codePointCount(0, failureSummary.length) <= 512)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `late stop reconciliation cannot resurrect pending after authoritative idle`() = runTest {
        val pendingStatus = CompletableDeferred<SessionControllerResult<SessionCommandStatus>>()
        val fixture = Fixture(
            scope = this,
            interrupt = { SessionControllerResult.Timeout },
            statusHandler = { pendingStatus.await() },
        )
        fixture.openController()
        fixture.setRunning(true)

        fixture.viewModel.stopCurrentTurn()
        runCurrent()
        assertTrue(fixture.viewModel.state.value.isInterrupting)

        fixture.setRunning(false)
        assertTrue(!fixture.viewModel.state.value.isInterrupting)

        pendingStatus.complete(
            SessionControllerResult.Success(
                SessionCommandStatus(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = ClientRequestId("request-1"),
                ),
            ),
        )
        runCurrent()

        try {
            assertTrue(!fixture.viewModel.state.value.isInterrupting)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unknown stop delivery reconciles the original request id without interrupting twice`() = runTest {
        val fixture = Fixture(
            scope = this,
            interrupt = { SessionControllerResult.Timeout },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = requestId,
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.stopCurrentTurn()
            runCurrent()

            assertEquals(1, fixture.channel.interruptCalls.size)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
            assertEquals(
                CommandPhase.ACCEPTED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            assertTrue(fixture.viewModel.state.value.isInterrupting)

            fixture.setRunning(false)
            assertTrue(!fixture.viewModel.state.value.isInterrupting)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unknown stop ignores rejected command status for a different request`() = runTest {
        val fixture = Fixture(
            scope = this,
            interrupt = { SessionControllerResult.Timeout },
            statusHandler = {
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.REJECTED,
                        clientRequestId = ClientRequestId("different-request"),
                    ),
                )
            },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.stopCurrentTurn()
            runCurrent()

            assertEquals(
                setOf(ClientRequestId("request-1")),
                fixture.viewModel.state.value.commands.commands.keys,
            )
            assertEquals(
                CommandPhase.UNKNOWN,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            assertTrue(fixture.viewModel.state.value.isInterrupting)
            assertTrue(!fixture.viewModel.state.value.canStop)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `malformed stop response after commit stays pending until status reconciliation`() = runTest {
        val pendingStatus = CompletableDeferred<SessionControllerResult<SessionCommandStatus>>()
        val fixture = Fixture(
            scope = this,
            interrupt = { SessionControllerResult.InvalidResponse },
            statusHandler = { pendingStatus.await() },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.stopCurrentTurn()
            runCurrent()

            assertEquals(1, fixture.channel.interruptCalls.size)
            assertEquals(
                listOf(ClientRequestId("request-1")),
                fixture.channel.commandStatusCalls,
            )
            assertTrue(fixture.viewModel.state.value.isInterrupting)
            assertTrue(!fixture.viewModel.state.value.canStop)
            assertEquals(
                CommandPhase.UNKNOWN,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )

            pendingStatus.complete(
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.ACCEPTED,
                        clientRequestId = ClientRequestId("request-1"),
                    ),
                ),
            )
            runCurrent()

            assertEquals(1, fixture.channel.interruptCalls.size)
            assertTrue(fixture.viewModel.state.value.isInterrupting)
            assertEquals(
                CommandPhase.ACCEPTED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )

            fixture.setRunning(false)
            assertTrue(!fixture.viewModel.state.value.isInterrupting)
        } finally {
            pendingStatus.complete(SessionControllerResult.InvalidResponse)
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `unadvertised prompt interrupt and guide mutations stay disabled and send nothing`() = runTest {
        val fixture = Fixture(
            scope = this,
            availableMethods = setOf(MobileControlMethods.ACQUIRE),
        )
        fixture.openController()
        fixture.setRunning(true)

        fixture.viewModel.onDraftChanged("Do not send")
        fixture.viewModel.onGuidanceDraftChanged("Do not steer")

        assertFalse(fixture.viewModel.state.value.canSend)
        assertFalse(fixture.viewModel.state.value.canStop)
        assertFalse(fixture.viewModel.state.value.canGuide)

        fixture.viewModel.sendPrompt()
        fixture.viewModel.stopCurrentTurn()
        fixture.viewModel.submitGuidance()
        runCurrent()

        assertTrue(fixture.channel.submitCalls.isEmpty())
        assertTrue(fixture.channel.interruptCalls.isEmpty())
        assertTrue(fixture.channel.steerCalls.isEmpty())
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `pending input remains visible but read only when response method is unavailable`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            availableMethods = setOf(MobileControlMethods.ACQUIRE),
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
        )
        fixture.openController()

        assertEquals(
            pending,
            (fixture.viewModel.state.value.control.mode as ControlMode.Controller)
                .lease
                .pendingInput,
        )
        assertFalse(fixture.viewModel.state.value.canRespondToPendingInput)

        fixture.viewModel.selectPendingChoice(SessionApprovalChoice.DENY.wireValue)
        fixture.viewModel.submitPendingInput()
        runCurrent()

        assertEquals(null, fixture.viewModel.state.value.pendingInteraction.selectedChoiceId)
        assertTrue(fixture.channel.approvalCalls.isEmpty())
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    @Test
    fun `desktop owner status blocks mobile acquire and remains a truthful conflict`() = runTest {
        val fixture = Fixture(
            scope = this,
            controlStatusHandler = {
                SessionControllerResult.Success(
                    app.hermesmobile.protocol.gateway.SessionControlStatus(
                        controllerKind = SessionControllerKind.DESKTOP,
                        controllerLabel = "Hermes Desktop",
                        controlRevision = 7,
                        leaseExpiresAtEpochMs = 0,
                        pendingInput = null,
                    ),
                )
            },
        )
        fixture.openController()

        try {
            assertEquals(1, fixture.channel.controlStatusCalls)
            assertEquals(0, fixture.channel.acquireCalls)
            val conflict = assertIs<app.hermesmobile.sessions.control.ControlMode.Conflict>(
                fixture.viewModel.state.value.control.mode,
            )
            assertEquals(SessionControllerKind.DESKTOP, conflict.controllerKind)
            assertEquals("Hermes Desktop", conflict.controllerLabel)
            assertFalse(fixture.viewModel.state.value.control.canMutate)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `effect unknown prompt reconciles the original request and never submits twice`() = runTest {
        val fixture = Fixture(
            scope = this,
            submit = { rpcFailure(4307, "effect_unknown") },
            statusHandler = { requestId ->
                SessionControllerResult.Success(
                    SessionCommandStatus(
                        status = SessionCommandState.UNKNOWN,
                        clientRequestId = requestId,
                        clientTurnId = ClientTurnId("turn-1"),
                    ),
                )
            },
        )
        fixture.openController()

        try {
            fixture.viewModel.onDraftChanged("do not duplicate this prompt")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals(1, fixture.channel.submitCalls.size)
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)
            assertEquals(
                listOf(MobileControlMethods.PROMPT_SUBMIT),
                fixture.channel.commandStatusMethodCalls,
            )
            assertEquals(
                CommandPhase.UNKNOWN,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            fixture.viewModel.sendPrompt()
            runCurrent()
            assertEquals(1, fixture.channel.submitCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `deadline before effect is definitive and never triggers command reconciliation`() = runTest {
        val fixture = Fixture(
            scope = this,
            submit = { rpcFailure(4306, "deadline_exceeded_before_effect") },
        )
        fixture.openController()

        try {
            fixture.viewModel.onDraftChanged("safe to retry manually")
            fixture.viewModel.sendPrompt()
            runCurrent()

            assertEquals(1, fixture.channel.submitCalls.size)
            assertTrue(fixture.channel.commandStatusCalls.isEmpty())
            assertEquals(
                CommandPhase.FAILED,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            assertTrue(fixture.viewModel.state.value.canSend)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `effect unknown guidance reconciles without steering twice`() = runTest {
        val fixture = Fixture(
            scope = this,
            steer = { rpcFailure(4307, "effect_unknown") },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.onGuidanceDraftChanged("preserve exact guidance request")
            fixture.viewModel.submitGuidance()
            runCurrent()

            assertEquals(1, fixture.channel.steerCalls.size)
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)
            assertEquals(
                SessionGuidancePhase.DELIVERY_UNKNOWN,
                fixture.viewModel.state.value.guidance.phase,
            )
            fixture.viewModel.submitGuidance()
            runCurrent()
            assertEquals(1, fixture.channel.steerCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `effect unknown interrupt reconciles without interrupting twice`() = runTest {
        val fixture = Fixture(
            scope = this,
            interrupt = { rpcFailure(4307, "effect_unknown") },
        )
        fixture.openController()
        fixture.setRunning(true)

        try {
            fixture.viewModel.stopCurrentTurn()
            runCurrent()

            assertEquals(1, fixture.channel.interruptCalls.size)
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)
            assertEquals(
                CommandPhase.UNKNOWN,
                fixture.viewModel.state.value.commands.commands
                    .getValue(ClientRequestId("request-1"))
                    .phase,
            )
            fixture.viewModel.stopCurrentTurn()
            runCurrent()
            assertEquals(1, fixture.channel.interruptCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `effect unknown approval reconciles status before allowing an explicit retry`() = runTest {
        val pending = approvalPending(SessionApprovalChoice.DENY)
        val fixture = Fixture(
            scope = this,
            acquireHandler = { SessionControllerResult.Success(controlLease(pending)) },
            renewHandler = { leaseId ->
                SessionControllerResult.Success(
                    controlLease(pending = pending, revision = 2, leaseId = leaseId),
                )
            },
            respondApproval = { rpcFailure(4307, "effect_unknown") },
        )
        fixture.openController()

        try {
            fixture.viewModel.selectPendingChoice("deny")
            fixture.viewModel.submitPendingInput()
            runCurrent()

            assertEquals(1, fixture.channel.approvalCalls.size)
            assertEquals(listOf(ClientRequestId("request-1")), fixture.channel.commandStatusCalls)
            assertEquals(
                PendingInputInteractionOutcome.RetryAvailable,
                fixture.viewModel.state.value.pendingInteraction.outcome,
            )
            assertEquals(1, fixture.channel.approvalCalls.size)
        } finally {
            fixture.viewModel.backToSessions()
            runCurrent()
        }
    }

    @Test
    fun `control reconnect replaces advertised mutation capabilities`() = runTest {
        val fixture = Fixture(scope = this)
        fixture.openController()
        fixture.viewModel.onDraftChanged("Initially available")
        assertTrue(fixture.viewModel.state.value.canSend)

        fixture.channel.transportEvents.emit(SessionControlTransportEvent.Closed("network"))
        runCurrent()
        assertTrue(fixture.viewModel.state.value.controlAvailableMethods.isEmpty())

        fixture.channel.availableMethods = setOf(MobileControlMethods.ACQUIRE)
        fixture.viewModel.retryControlConnection()
        runCurrent()

        assertEquals(
            setOf(MobileControlMethods.ACQUIRE),
            fixture.viewModel.state.value.controlAvailableMethods,
        )
        assertFalse(fixture.viewModel.state.value.canSend)
        fixture.viewModel.backToSessions()
        runCurrent()
    }

    private class Fixture(
        private val scope: kotlinx.coroutines.test.TestScope,
        submit: suspend (SubmitCall) -> SessionControllerResult<PromptSubmitResponse> = { call ->
            SessionControllerResult.Success(
                PromptSubmitResponse(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = call.requestId,
                    clientTurnId = call.clientTurnId,
                ),
            )
        },
        statusHandler: suspend (ClientRequestId) -> SessionControllerResult<SessionCommandStatus> = {
            SessionControllerResult.RpcFailure(
                app.hermesmobile.protocol.gateway.JsonRpcError(
                    4210,
                    "command status unknown",
                    null,
                ),
            )
        },
        leaseExpiresAtEpochMs: Long = 9_000_000_000_000L,
        clockEpochMs: () -> Long = { 1_900_000_000_000L },
        leaseRenewLeadMillis: Long = 5_000L,
        interrupt: suspend (InterruptCall) -> SessionControllerResult<SessionInterruptResponse> = { call ->
            SessionControllerResult.Success(
                SessionInterruptResponse(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = call.requestId,
                ),
            )
        },
        steer: suspend (SteerCall) -> SessionControllerResult<SessionSteerResponse> = { call ->
            SessionControllerResult.Success(
                SessionSteerResponse(
                    status = SessionCommandState.ACCEPTED,
                    clientRequestId = call.requestId,
                ),
            )
        },
        respondApproval: suspend (ApprovalCall) -> SessionControllerResult<PendingInputRespondResponse> = {
            SessionControllerResult.Unsupported
        },
        respondClarify: suspend (ClarifyCall) -> SessionControllerResult<PendingInputRespondResponse> = {
            SessionControllerResult.Unsupported
        },
        controlOpenGate: CompletableDeferred<Unit>? = null,
        controlOpenIgnoresCancellation: Boolean = false,
        controlOpenResult: SessionControlOpenResult? = null,
        initialTranscriptOffset: Int = 0,
        authenticateAfterInitialTranscript: Boolean = false,
        acquireHandler: (suspend () -> SessionControllerResult<SessionControlLease>)? = null,
        controlStatusHandler: suspend () -> SessionControllerResult<
            app.hermesmobile.protocol.gateway.SessionControlStatus
            > = {
            SessionControllerResult.Success(
                app.hermesmobile.protocol.gateway.SessionControlStatus(
                    controllerKind = SessionControllerKind.NONE,
                    controllerLabel = null,
                    controlRevision = 0,
                    leaseExpiresAtEpochMs = 0,
                    pendingInput = null,
                ),
            )
        },
        renewHandler: (suspend (SessionControlLeaseId) -> SessionControllerResult<SessionControlLease>)? = null,
        availableMethods: Set<String> = MobileControlMethods.IMPLEMENTED,
    ) {
        val session = session()
        val transcript = transcript().copy(
            pagination = transcript().pagination.copy(offset = initialTranscriptOffset),
        )
        val realtime = FakeRealtimeSource()
        val channel = FakeControlChannel(
            leaseExpiresAtEpochMs = leaseExpiresAtEpochMs,
            submit = submit,
            statusHandler = statusHandler,
            interrupt = interrupt,
            steer = steer,
            respondApproval = respondApproval,
            respondClarify = respondClarify,
            acquireHandler = acquireHandler,
            controlStatusHandler = controlStatusHandler,
            renewHandler = renewHandler,
            availableMethods = availableMethods,
        )
        val control = FakeControlSource(
            channel = channel,
            openGate = controlOpenGate,
            ignoreCancellation = controlOpenIgnoresCancellation,
            openResult = controlOpenResult,
        )
        val viewModel = SessionBrowserViewModel(
            source = FakeSource(
                session,
                transcript,
                authenticateAfterInitialTranscript,
            ),
            realtimeSource = realtime,
            controlSource = control,
            requestIdFactory = { ClientRequestId("request-1") },
            turnIdFactory = { ClientTurnId("turn-1") },
            clockEpochMs = clockEpochMs,
            leaseRenewLeadMillis = leaseRenewLeadMillis,
        )

        fun open() {
            viewModel.start()
            scope.runCurrent()
            viewModel.openSession(session.sessionKey)
            scope.runCurrent()
        }

        fun openController() {
            open()
            scope.runCurrent()
            runBlockingEmit(
                SessionRealtimeUpdate.Projection(
                    RealtimeSessionReducer().seed(
                        transcript,
                        RuntimeSessionId("runtime-1"),
                        connectionEpoch = 1,
                    ),
                ),
            )
            scope.runCurrent()
        }

        fun setRunning(running: Boolean) {
            val projection = requireNotNull(viewModel.state.value.realtime).copy(running = running)
            runBlockingEmit(SessionRealtimeUpdate.Projection(projection))
            scope.runCurrent()
        }

        fun emitProjection(runtimeSessionId: RuntimeSessionId, running: Boolean) {
            val projection = RealtimeSessionReducer().seed(
                transcript,
                runtimeSessionId,
                connectionEpoch = 2,
            ).copy(running = running)
            runBlockingEmit(SessionRealtimeUpdate.Projection(projection))
            scope.runCurrent()
        }

        fun emitRuntime(runtimeSessionId: RuntimeSessionId) {
            val projection = requireNotNull(viewModel.state.value.realtime).copy(
                runtimeSessionId = runtimeSessionId,
            )
            runBlockingEmit(SessionRealtimeUpdate.Projection(projection))
            scope.runCurrent()
        }

        private fun runBlockingEmit(update: SessionRealtimeUpdate) {
            kotlinx.coroutines.runBlocking { realtime.updates.emit(update) }
        }
    }

    private class FakeControlSource(
        private val channel: SessionControlChannel,
        private val openGate: CompletableDeferred<Unit>? = null,
        private val ignoreCancellation: Boolean = false,
        private val openResult: SessionControlOpenResult? = null,
    ) : SessionControlSource {
        val openRequests = mutableListOf<Pair<SessionProjection, RuntimeSessionId>>()

        override suspend fun open(
            session: SessionProjection,
            runtimeSessionId: RuntimeSessionId,
        ): SessionControlOpenResult {
            openRequests += session to runtimeSessionId
            if (ignoreCancellation) {
                withContext(NonCancellable) { openGate?.await() }
            } else {
                openGate?.await()
            }
            return openResult ?: SessionControlOpenResult.Ready(channel)
        }
    }

    private class FakeControlChannel(
        private val submit: suspend (SubmitCall) -> SessionControllerResult<PromptSubmitResponse>,
        private val statusHandler: suspend (ClientRequestId) -> SessionControllerResult<SessionCommandStatus>,
        private val leaseExpiresAtEpochMs: Long,
        private val interrupt: suspend (InterruptCall) -> SessionControllerResult<SessionInterruptResponse>,
        private val steer: suspend (SteerCall) -> SessionControllerResult<SessionSteerResponse>,
        private val respondApproval: suspend (ApprovalCall) -> SessionControllerResult<PendingInputRespondResponse>,
        private val respondClarify: suspend (ClarifyCall) -> SessionControllerResult<PendingInputRespondResponse>,
        private val acquireHandler: (suspend () -> SessionControllerResult<SessionControlLease>)?,
        private val controlStatusHandler: suspend () -> SessionControllerResult<
            app.hermesmobile.protocol.gateway.SessionControlStatus
            >,
        private val renewHandler: (suspend (SessionControlLeaseId) -> SessionControllerResult<SessionControlLease>)?,
        availableMethods: Set<String>,
    ) : SessionControlChannel {
        override var availableMethods: Set<String> = availableMethods
        val transportEvents = MutableSharedFlow<SessionControlTransportEvent>(extraBufferCapacity = 8)
        override val events: Flow<SessionControlTransportEvent> = transportEvents
        val submitCalls = mutableListOf<SubmitCall>()
        val commandStatusCalls = mutableListOf<ClientRequestId>()
        val commandStatusMethodCalls = mutableListOf<String>()
        val releaseCalls = mutableListOf<SessionControlLeaseId>()
        val renewCalls = mutableListOf<SessionControlLeaseId>()
        val interruptCalls = mutableListOf<InterruptCall>()
        val steerCalls = mutableListOf<SteerCall>()
        val approvalCalls = mutableListOf<ApprovalCall>()
        val clarifyCalls = mutableListOf<ClarifyCall>()
        var acquireCalls = 0
        var controlStatusCalls = 0
        var closed = false

        override suspend fun acquire(): SessionControllerResult<SessionControlLease> {
            acquireCalls += 1
            acquireHandler?.let { return it() }
            return SessionControllerResult.Success(
                SessionControlLease(
                    leaseId = SessionControlLeaseId("lease-1"),
                    expiresAtEpochMs = leaseExpiresAtEpochMs,
                    controlRevision = 1,
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = "Hermes Mobile",
                    pendingInput = null,
                ),
            )
        }

        override suspend fun status(): SessionControllerResult<
            app.hermesmobile.protocol.gateway.SessionControlStatus
            > {
            controlStatusCalls += 1
            return controlStatusHandler()
        }

        override suspend fun renew(
            leaseId: SessionControlLeaseId,
        ): SessionControllerResult<SessionControlLease> {
            renewCalls += leaseId
            renewHandler?.let { return it(leaseId) }
            return SessionControllerResult.Success(
                SessionControlLease(
                    leaseId = leaseId,
                    expiresAtEpochMs = leaseExpiresAtEpochMs + 2_000L,
                    controlRevision = 2,
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = "Hermes Mobile",
                    pendingInput = null,
                ),
            )
        }

        override suspend fun release(
            leaseId: SessionControlLeaseId,
        ): SessionControllerResult<SessionControlReleaseResponse> {
            releaseCalls += leaseId
            return SessionControllerResult.Success(
                SessionControlReleaseResponse(released = true, controlRevision = 2),
            )
        }

        override suspend fun commandStatus(
            method: String,
            requestId: ClientRequestId,
        ): SessionControllerResult<SessionCommandStatus> {
            commandStatusMethodCalls += method
            commandStatusCalls += requestId
            return statusHandler(requestId)
        }

        override suspend fun submitPrompt(
            leaseId: SessionControlLeaseId,
            requestId: ClientRequestId,
            clientTurnId: ClientTurnId,
            text: String,
        ): SessionControllerResult<PromptSubmitResponse> {
            val call = SubmitCall(leaseId, requestId, clientTurnId, text)
            submitCalls += call
            return submit(call)
        }

        override suspend fun interrupt(
            leaseId: SessionControlLeaseId,
            requestId: ClientRequestId,
        ): SessionControllerResult<SessionInterruptResponse> {
            val call = InterruptCall(leaseId, requestId)
            interruptCalls += call
            return interrupt(call)
        }

        override suspend fun steer(
            leaseId: SessionControlLeaseId,
            requestId: ClientRequestId,
            text: String,
        ): SessionControllerResult<SessionSteerResponse> {
            val call = SteerCall(leaseId, requestId, text)
            steerCalls += call
            return steer(call)
        }

        override suspend fun respondApproval(
            leaseId: SessionControlLeaseId,
            clientRequestId: ClientRequestId,
            requestId: String,
            choice: SessionApprovalChoice,
        ): SessionControllerResult<PendingInputRespondResponse> {
            val call = ApprovalCall(leaseId, clientRequestId, requestId, choice)
            approvalCalls += call
            return respondApproval(call)
        }

        override suspend fun respondClarify(
            leaseId: SessionControlLeaseId,
            clientRequestId: ClientRequestId,
            requestId: String,
            answer: SessionClarifyAnswer,
        ): SessionControllerResult<PendingInputRespondResponse> {
            val call = ClarifyCall(leaseId, clientRequestId, requestId, answer)
            clarifyCalls += call
            return respondClarify(call)
        }

        override fun close() {
            closed = true
        }
    }

    private class FakeRealtimeSource : SessionRealtimeSource {
        val updates = MutableSharedFlow<SessionRealtimeUpdate>(extraBufferCapacity = 8)

        override fun observe(
            session: SessionProjection,
            baseline: SessionTranscript,
        ): Flow<SessionRealtimeUpdate> = updates
    }

    private class FakeSource(
        private val session: SessionProjection,
        private val transcript: SessionTranscript,
        private val authenticateAfterInitialTranscript: Boolean,
    ) : SessionBrowserSource {
        private var messageRequestCount = 0

        override suspend fun loadSessions(
            limit: Int,
            offset: Int,
            profile: String?,
        ) = SessionRepositoryResult.Data(SessionPage(listOf(session), 1, limit, offset))

        override suspend fun loadMessages(
            sessionKey: SessionKey,
            limit: Int,
            offset: Int,
            profile: String?,
        ): SessionRepositoryResult<SessionTranscript> {
            messageRequestCount += 1
            if (authenticateAfterInitialTranscript && messageRequestCount > 1) {
                return SessionRepositoryResult.AuthenticationRequired
            }
            return SessionRepositoryResult.Data(transcript)
        }
    }

    private data class SubmitCall(
        val leaseId: SessionControlLeaseId,
        val requestId: ClientRequestId,
        val clientTurnId: ClientTurnId,
        val text: String,
    )

    private data class InterruptCall(
        val leaseId: SessionControlLeaseId,
        val requestId: ClientRequestId,
    )

    private data class SteerCall(
        val leaseId: SessionControlLeaseId,
        val requestId: ClientRequestId,
        val text: String,
    )

    private data class ApprovalCall(
        val leaseId: SessionControlLeaseId,
        val clientRequestId: ClientRequestId,
        val requestId: String,
        val choice: SessionApprovalChoice,
    )

    private data class ClarifyCall(
        val leaseId: SessionControlLeaseId,
        val clientRequestId: ClientRequestId,
        val requestId: String,
        val answer: SessionClarifyAnswer,
    )

    private companion object {
        fun approvalPending(vararg choices: SessionApprovalChoice) = SessionPendingInput.Approval(
            requestId = "approval-1",
            title = "Run command?",
            description = "",
            command = "pwd",
            choices = choices.toList(),
            expiresAtEpochMs = 9_000_000_000_000L,
        )

        fun controlLease(
            pending: SessionPendingInput?,
            revision: Long = 1L,
            leaseId: SessionControlLeaseId = SessionControlLeaseId("lease-1"),
        ) = SessionControlLease(
            leaseId = leaseId,
            expiresAtEpochMs = 9_000_000_000_000L,
            controlRevision = revision,
            controllerKind = SessionControllerKind.MOBILE,
            controllerLabel = "Hermes Mobile",
            pendingInput = pending,
        )

        fun accepted(
            requestId: String,
            clientRequestId: ClientRequestId,
            kind: PendingInputKind,
        ): SessionControllerResult<PendingInputRespondResponse> = SessionControllerResult.Success(
            PendingInputRespondResponse(
                kind = kind,
                requestId = requestId,
                clientRequestId = clientRequestId,
                controlRevision = 2L,
            ),
        )

        fun <T> rpcFailure(code: Int, reason: String): SessionControllerResult<T> =
            SessionControllerResult.RpcFailure(
                app.hermesmobile.protocol.gateway.JsonRpcError(
                    code = code,
                    message = reason,
                    data = null,
                ),
            )

        fun session() = SessionProjection(
            sessionKey = SessionKey("stored-1"),
            lineageRoot = SessionKey("stored-1"),
            lineageTip = SessionKey("stored-1"),
            parentSessionKey = null,
            title = "First session",
            preview = "Preview",
            source = "desktop",
            model = "test-model",
            profile = "default",
            cwd = null,
            gitBranch = null,
            startedAtEpochSeconds = 100.0,
            endedAtEpochSeconds = null,
            lastActiveEpochSeconds = 120.0,
            messageCount = 1,
            toolCallCount = 0,
            inputTokens = 0,
            outputTokens = 0,
            isActive = true,
            archived = false,
        )

        fun transcript() = SessionTranscript(
            sessionKey = SessionKey("stored-1"),
            lineageTip = SessionKey("stored-1"),
            messages = listOf(
                SessionMessageProjection(
                    messageId = 1,
                    role = "user",
                    content = JsonPrimitive("hello"),
                    timestampEpochSeconds = 100.0,
                    reasoning = null,
                    reasoningContent = null,
                    reasoningDetails = null,
                    toolCallId = null,
                    toolCalls = null,
                    toolName = null,
                    displayKind = null,
                    displayMetadata = null,
                ),
            ),
            pagination = TranscriptPagination(limit = 20, offset = 0, returned = 1),
        )
    }
}
