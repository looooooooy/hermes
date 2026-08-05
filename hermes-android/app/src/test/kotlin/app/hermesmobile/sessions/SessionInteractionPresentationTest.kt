package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyChoice
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.sessions.control.CommandPhase
import app.hermesmobile.sessions.control.CommandRecord
import app.hermesmobile.sessions.control.CommandState
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.ControlState
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import app.hermesmobile.sessions.control.PendingInputInteractionState
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class SessionInteractionPresentationTest {
    @Test
    fun `pending request replaces the ordinary composer with one decision surface`() {
        assertEquals(
            SessionBottomInputSurface.Decision,
            sessionBottomInputSurface(approvalPending()),
        )
    }

    @Test
    fun `authoritative lease pending input remains visible independent of local interaction identity`() {
        val pending = approvalPending()
        val control = ControlState(
            mode = ControlMode.Controller(
                SessionControlLease(
                    leaseId = SessionControlLeaseId("lease-1"),
                    expiresAtEpochMs = 9_000_000_000_000L,
                    controlRevision = 1L,
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = "Mobile",
                    pendingInput = pending,
                ),
            ),
        )

        assertEquals(pending, authoritativePendingInput(control))
    }

    @Test
    fun `running controller exposes Guide as a composer action`() {
        assertTrue(
            sessionComposerGuidanceActionVisible(
                running = true,
                canMutate = true,
                isInterrupting = false,
            ),
        )
    }

    @Test
    fun `durable approval choice enters explicit confirmation before submission`() {
        val presentation = pendingInputDockPresentation(
            pendingInput = approvalPending(),
            interaction = PendingInputInteractionState(
                requestId = "approval-1",
                selectedChoiceId = SessionApprovalChoice.ALLOW_ALWAYS.wireValue,
                requiresConfirmation = true,
            ),
        )

        assertEquals(PendingInputDockMode.Approval, presentation.mode)
        assertTrue(presentation.showsConfirmation)
        assertFalse(presentation.showsChoices)
        assertFalse(presentation.submitEnabled)
        assertFalse(presentation.retryEnabled)
    }

    @Test
    fun `authoritative pending request stays read only while local identity is stale`() {
        val presentation = pendingInputDockPresentation(
            pendingInput = approvalPending(),
            interaction = PendingInputInteractionState(
                requestId = "previous-request",
                selectedChoiceId = SessionApprovalChoice.ALLOW_ONCE.wireValue,
            ),
        )

        assertFalse(presentation.editingEnabled)
        assertFalse(presentation.submitEnabled)
        assertFalse(presentation.retryEnabled)
    }

    @Test
    fun `unavailable response capability keeps authoritative pending request read only`() {
        val presentation = pendingInputDockPresentation(
            pendingInput = clarifyPending(),
            interaction = PendingInputInteractionState(
                requestId = "clarify-1",
                selectedChoiceId = "staging",
            ),
            mutationEnabled = false,
        )

        assertFalse(presentation.editingEnabled)
        assertFalse(presentation.submitEnabled)
        assertFalse(presentation.retryEnabled)
    }

    @Test
    fun `clarify selection is visible and enables exactly one answer submission`() {
        val presentation = pendingInputDockPresentation(
            pendingInput = clarifyPending(),
            interaction = PendingInputInteractionState(
                requestId = "clarify-1",
                selectedChoiceId = "staging",
            ),
        )

        assertEquals(PendingInputDockMode.Clarify, presentation.mode)
        assertEquals("staging", presentation.selectedChoiceId)
        assertTrue(presentation.editingEnabled)
        assertTrue(presentation.submitEnabled)
        assertFalse(presentation.retryEnabled)
    }

    @Test
    fun `same-request retry is restored and freezes the accepted payload identity`() {
        val presentation = pendingInputDockPresentation(
            pendingInput = approvalPending(),
            interaction = PendingInputInteractionState(
                requestId = "approval-1",
                selectedChoiceId = SessionApprovalChoice.ALLOW_ONCE.wireValue,
                outcome = PendingInputInteractionOutcome.RetryAvailable,
            ),
        )

        assertEquals(PendingInputDockMode.Restored, presentation.mode)
        assertFalse(presentation.editingEnabled)
        assertTrue(presentation.showsChoices)
        assertTrue(presentation.retryEnabled)
        assertFalse(presentation.submitEnabled)
    }

    @Test
    fun `stale restored retry remains read only for a different authoritative request`() {
        val presentation = pendingInputDockPresentation(
            pendingInput = approvalPending(),
            interaction = PendingInputInteractionState(
                requestId = "previous-request",
                selectedChoiceId = SessionApprovalChoice.ALLOW_ONCE.wireValue,
                outcome = PendingInputInteractionOutcome.RetryAvailable,
            ),
        )

        assertEquals(PendingInputDockMode.Restored, presentation.mode)
        assertFalse(presentation.editingEnabled)
        assertFalse(presentation.retryEnabled)
        assertFalse(presentation.submitEnabled)
    }

    @Test
    fun `pending feedback announces uncertainty politely and failures assertively`() {
        val unknown = pendingInputFeedbackSemantics(PendingInputInteractionOutcome.DeliveryUnknown)
        val retry = pendingInputFeedbackSemantics(PendingInputInteractionOutcome.RetryAvailable)
        val resolved = pendingInputFeedbackSemantics(PendingInputInteractionOutcome.ResolvedElsewhere)
        val failed = pendingInputFeedbackSemantics(PendingInputInteractionOutcome.Failed("Try again"))

        assertEquals(PendingInputFeedbackAnnouncement.Polite, unknown?.announcement)
        assertFalse(requireNotNull(unknown).isError)
        assertEquals(PendingInputFeedbackAnnouncement.Polite, retry?.announcement)
        assertEquals(PendingInputFeedbackAnnouncement.Polite, resolved?.announcement)
        assertEquals(PendingInputFeedbackAnnouncement.Assertive, failed?.announcement)
        assertTrue(requireNotNull(failed).isError)
        assertEquals(null, pendingInputFeedbackSemantics(PendingInputInteractionOutcome.Accepted))
        assertEquals(null, pendingInputFeedbackSemantics(null))
    }

    @Test
    fun `pending choices expose a 48dp target and durable choice confirmation state`() {
        val allowOnce = approvalChoicePresentation(SessionApprovalChoice.ALLOW_ONCE)
        val allowAlways = approvalChoicePresentation(SessionApprovalChoice.ALLOW_ALWAYS)

        assertEquals(48, PENDING_INPUT_MIN_TOUCH_TARGET_DP)
        assertTrue(allowOnce.submitsImmediately)
        assertFalse(allowOnce.requiresConfirmation)
        assertFalse(allowAlways.submitsImmediately)
        assertTrue(allowAlways.requiresConfirmation)
    }

    @Test
    fun `ready composer exposes button and keyboard send through one presentation`() {
        val presentation = transcriptComposerPresentation(
            running = false,
            isInterrupting = false,
            canEdit = true,
            canSend = true,
            canStop = false,
            hasDraft = true,
        )

        assertEquals(TranscriptComposerPrimaryAction.Send, presentation.primaryAction)
        assertTrue(presentation.inputEnabled)
        assertTrue(presentation.primaryEnabled)
        assertTrue(presentation.keyboardSendEnabled)
    }

    @Test
    fun `running composer queues the draft while keeping Stop independently available`() {
        val presentation = transcriptComposerPresentation(
            running = true,
            isInterrupting = false,
            canEdit = true,
            canSend = true,
            canStop = true,
            hasDraft = true,
        )

        assertEquals(TranscriptComposerPrimaryAction.Queue, presentation.primaryAction)
        assertTrue(presentation.inputEnabled)
        assertTrue(presentation.primaryEnabled)
        assertTrue(presentation.keyboardSendEnabled)
        assertTrue(presentation.stopActionVisible)
        assertTrue(presentation.stopEnabled)
    }

    @Test
    fun `queue window contains only the latest three server acknowledged prompts`() {
        val commands = CommandState(
            commands = linkedMapOf(
                queuedCommand(1) to queuedRecord(1),
                queuedCommand(2) to queuedRecord(2),
                queuedCommand(3) to queuedRecord(3),
                queuedCommand(4) to queuedRecord(4),
                ClientRequestId("sending") to CommandRecord(
                    requestId = ClientRequestId("sending"),
                    clientTurnId = ClientTurnId("turn-sending"),
                    phase = CommandPhase.SENDING,
                    promptPreview = "Not acknowledged",
                ),
            ),
        )

        val window = queuedPromptWindow(commands)

        assertEquals(4, window.totalCount)
        assertEquals(1, window.hiddenBeforeCount)
        assertEquals(
            listOf("Queued prompt 2", "Queued prompt 3", "Queued prompt 4"),
            window.items.map(QueuedPromptPresentation::preview),
        )
    }

    @Test
    fun `pending input disables ordinary composer button and keyboard send`() {
        val presentation = transcriptComposerPresentation(
            running = false,
            isInterrupting = false,
            canEdit = false,
            canSend = false,
            canStop = false,
            hasDraft = true,
        )

        assertEquals(TranscriptComposerPrimaryAction.Send, presentation.primaryAction)
        assertFalse(presentation.inputEnabled)
        assertFalse(presentation.primaryEnabled)
        assertFalse(presentation.keyboardSendEnabled)
    }

    private fun approvalPending() = SessionPendingInput.Approval(
        requestId = "approval-1",
        title = "Run command?",
        description = "Hermes needs permission.",
        command = "./gradlew test",
        choices = listOf(
            SessionApprovalChoice.ALLOW_ONCE,
            SessionApprovalChoice.ALLOW_ALWAYS,
            SessionApprovalChoice.DENY,
        ),
        expiresAtEpochMs = 9_000_000_000_000L,
    )

    private fun clarifyPending() = SessionPendingInput.Clarify(
        requestId = "clarify-1",
        question = "Which target?",
        choices = listOf(
            SessionClarifyChoice("staging", "Staging"),
            SessionClarifyChoice("production", "Production"),
        ),
        allowOther = true,
        expiresAtEpochMs = 9_000_000_000_000L,
    )

    private fun queuedCommand(index: Int) = ClientRequestId("request-$index")

    private fun queuedRecord(index: Int) = CommandRecord(
        requestId = queuedCommand(index),
        clientTurnId = ClientTurnId("turn-$index"),
        phase = CommandPhase.QUEUED,
        promptPreview = "Queued prompt $index",
    )
}
