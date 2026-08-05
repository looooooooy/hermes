package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.ClientRequestId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertSame
import kotlin.test.assertTrue

class PendingInputInteractionReducerTest {
    private val reducer = PendingInputInteractionReducer()

    @Test
    fun `same request restoration preserves other draft and new request resets it`() {
        val editing = reducer.reduce(
            reducer.reduce(
                reducer.reduce(PendingInputInteractionState(), PendingInputInteractionAction.Observed("clarify-1")),
                PendingInputInteractionAction.OtherDraftChanged("Custom answer"),
            ),
            PendingInputInteractionAction.ChoiceSelected("other", requiresConfirmation = false),
        )

        assertEquals(editing, reducer.reduce(editing, PendingInputInteractionAction.Observed("clarify-1")))

        val replaced = reducer.reduce(editing, PendingInputInteractionAction.Observed("clarify-2"))
        assertEquals("clarify-2", replaced.requestId)
        assertEquals("", replaced.otherDraft)
        assertEquals(null, replaced.selectedChoiceId)
        assertEquals(null, replaced.outcome)
    }

    @Test
    fun `always choice cannot dispatch before explicit confirmation`() {
        val observed = reducer.reduce(
            PendingInputInteractionState(),
            PendingInputInteractionAction.Observed("approval-1"),
        )
        val selected = reducer.reduce(
            observed,
            PendingInputInteractionAction.ChoiceSelected(
                choiceId = "allow_always",
                requiresConfirmation = true,
            ),
        )

        assertTrue(selected.requiresConfirmation)
        assertFalse(selected.canSubmit)

        val confirmed = reducer.reduce(selected, PendingInputInteractionAction.ConfirmationGranted)
        assertFalse(confirmed.requiresConfirmation)
        assertTrue(confirmed.canSubmit)

        val cancelled = reducer.reduce(selected, PendingInputInteractionAction.ConfirmationCancelled)
        assertFalse(cancelled.requiresConfirmation)
        assertEquals(null, cancelled.selectedChoiceId)
    }

    @Test
    fun `duplicate submission is single flight and retry requires authoritative same-request observation`() {
        val requestId = ClientRequestId("client-1")
        val ready = reducer.reduce(
            reducer.reduce(
                PendingInputInteractionState(),
                PendingInputInteractionAction.Observed("approval-1"),
            ),
            PendingInputInteractionAction.ChoiceSelected("allow_once", requiresConfirmation = false),
        )
        val submitting = reducer.reduce(
            ready,
            PendingInputInteractionAction.SubmissionStarted(requestId),
        )

        assertFalse(submitting.canSubmit)
        assertSame(
            submitting,
            reducer.reduce(
                submitting,
                PendingInputInteractionAction.SubmissionStarted(ClientRequestId("client-2")),
            ),
        )

        val unknown = reducer.reduce(
            submitting,
            PendingInputInteractionAction.DeliveryUnknown(requestId),
        )
        assertEquals(PendingInputInteractionOutcome.DeliveryUnknown, unknown.outcome)
        assertFalse(unknown.canSubmit)

        val reconciled = reducer.reduce(
            unknown,
            PendingInputInteractionAction.Observed("approval-1"),
        )
        assertEquals(PendingInputInteractionOutcome.RetryAvailable, reconciled.outcome)
        assertEquals(requestId, reconciled.inFlightClientRequestId)
        assertTrue(reconciled.canSubmit)

        val retried = reducer.reduce(
            reconciled,
            PendingInputInteractionAction.SubmissionStarted(requestId),
        )
        assertEquals(null, retried.outcome)
        assertEquals(requestId, retried.inFlightClientRequestId)
        assertFalse(retried.canSubmit)
    }

    @Test
    fun `definitive failure preserves draft and restores manual retry`() {
        val requestId = ClientRequestId("client-1")
        val editing = reducer.reduce(
            reducer.reduce(
                PendingInputInteractionState(),
                PendingInputInteractionAction.Observed("clarify-1"),
            ),
            PendingInputInteractionAction.OtherDraftChanged("Keep this answer"),
        )
        val submitting = reducer.reduce(
            editing,
            PendingInputInteractionAction.SubmissionStarted(requestId),
        )
        val failed = reducer.reduce(
            submitting,
            PendingInputInteractionAction.Failed(requestId, "Try again"),
        )

        assertEquals("Keep this answer", failed.otherDraft)
        assertEquals(PendingInputInteractionOutcome.Failed("Try again"), failed.outcome)
        assertTrue(failed.canSubmit)
    }

    @Test
    fun `reconciliation failure stays frozen until the same authoritative request is observed`() {
        val requestId = ClientRequestId("client-1")
        val submitting = reducer.reduce(
            reducer.reduce(
                reducer.reduce(
                    PendingInputInteractionState(),
                    PendingInputInteractionAction.Observed("clarify-1"),
                ),
                PendingInputInteractionAction.OtherDraftChanged("Keep this answer"),
            ),
            PendingInputInteractionAction.SubmissionStarted(requestId),
        )

        val reconciling = reducer.reduce(
            submitting,
            PendingInputInteractionAction.ReconciliationRequired(
                clientRequestId = requestId,
                summary = "Refresh before retrying",
                clearAnswer = false,
            ),
        )

        assertEquals("Keep this answer", reconciling.otherDraft)
        assertEquals(requestId, reconciling.inFlightClientRequestId)
        assertEquals(
            PendingInputInteractionOutcome.Failed("Refresh before retrying"),
            reconciling.outcome,
        )
        assertFalse(reconciling.canSubmit)

        val refreshed = reducer.reduce(
            reconciling,
            PendingInputInteractionAction.Observed("clarify-1"),
        )
        assertEquals(null, refreshed.inFlightClientRequestId)
        assertEquals("Keep this answer", refreshed.otherDraft)
        assertTrue(refreshed.canSubmit)

        val invalid = reducer.reduce(
            submitting,
            PendingInputInteractionAction.ReconciliationRequired(
                clientRequestId = requestId,
                summary = "Selection is invalid",
                clearAnswer = true,
            ),
        )
        assertEquals(null, invalid.selectedChoiceId)
        assertEquals("", invalid.otherDraft)
        assertFalse(invalid.canSubmit)
    }

    @Test
    fun `resolved elsewhere clears only the matching active request`() {
        val requestId = ClientRequestId("client-1")
        val submitting = reducer.reduce(
            reducer.reduce(
                PendingInputInteractionState(),
                PendingInputInteractionAction.Observed("approval-1"),
            ),
            PendingInputInteractionAction.SubmissionStarted(requestId),
        )

        val stale = reducer.reduce(
            submitting,
            PendingInputInteractionAction.ResolvedElsewhere(ClientRequestId("other")),
        )
        assertSame(submitting, stale)

        val resolved = reducer.reduce(
            submitting,
            PendingInputInteractionAction.ResolvedElsewhere(requestId),
        )
        assertEquals(null, resolved.requestId)
        assertEquals(PendingInputInteractionOutcome.ResolvedElsewhere, resolved.outcome)
    }

    @Test
    fun `accepted clears only the matching active request`() {
        val requestId = ClientRequestId("client-1")
        val submitting = reducer.reduce(
            reducer.reduce(
                reducer.reduce(
                    PendingInputInteractionState(),
                    PendingInputInteractionAction.Observed("clarify-1"),
                ),
                PendingInputInteractionAction.OtherDraftChanged("Frozen answer"),
            ),
            PendingInputInteractionAction.SubmissionStarted(requestId),
        )

        assertSame(
            submitting,
            reducer.reduce(
                submitting,
                PendingInputInteractionAction.Accepted(ClientRequestId("late-client")),
            ),
        )
        val accepted = reducer.reduce(
            submitting,
            PendingInputInteractionAction.Accepted(requestId),
        )
        assertEquals(null, accepted.requestId)
        assertEquals(null, accepted.inFlightClientRequestId)
        assertEquals("", accepted.otherDraft)
        assertEquals(PendingInputInteractionOutcome.Accepted, accepted.outcome)
    }
}
