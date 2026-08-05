package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class ComposerStateReducerTest {
    private val reducer = ComposerStateReducer()
    private val submission = ComposerSubmission(
        requestId = ClientRequestId("request-1"),
        clientTurnId = ClientTurnId("turn-1"),
        text = "Explain the failing test",
    )

    @Test
    fun `rejected submission becomes retryable without clearing its draft`() {
        val drafted = reducer.reduce(
            ComposerState(),
            ComposerAction.DraftChanged(submission.text),
        )
        val submitting = reducer.reduce(drafted, ComposerAction.SubmitStarted(submission))

        val rejected = reducer.reduce(
            submitting,
            ComposerAction.SubmissionRejected(submission.requestId),
        )

        assertEquals(submission.text, rejected.draft)
        assertNull(rejected.submitted)
        assertNull(rejected.lastAcknowledgedRequestId)
    }

    @Test
    fun `matching acknowledgement clears an unchanged submitted draft`() {
        val drafted = reducer.reduce(
            ComposerState(),
            ComposerAction.DraftChanged(submission.text),
        )
        val submitting = reducer.reduce(drafted, ComposerAction.SubmitStarted(submission))

        val acknowledged = reducer.reduce(
            submitting,
            ComposerAction.SubmissionAcknowledged(submission.requestId),
        )

        assertEquals("", acknowledged.draft)
        assertNull(acknowledged.submitted)
        assertEquals(submission.requestId, acknowledged.lastAcknowledgedRequestId)
    }
}
