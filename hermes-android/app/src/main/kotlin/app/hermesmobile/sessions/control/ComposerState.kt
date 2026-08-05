package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId

data class ComposerSubmission(
    val requestId: ClientRequestId,
    val clientTurnId: ClientTurnId,
    val text: String,
) {
    init {
        require(text.isNotBlank()) { "Submitted prompt must not be blank." }
    }
}

data class ComposerState(
    val draft: String = "",
    val submitted: ComposerSubmission? = null,
    val lastAcknowledgedRequestId: ClientRequestId? = null,
)

sealed interface ComposerAction {
    data class DraftChanged(val text: String) : ComposerAction
    data class SubmitStarted(val submission: ComposerSubmission) : ComposerAction
    data class SubmissionAcknowledged(val requestId: ClientRequestId) : ComposerAction
    data class SubmissionRejected(val requestId: ClientRequestId) : ComposerAction
}

class ComposerStateReducer {
    fun reduce(current: ComposerState, action: ComposerAction): ComposerState = when (action) {
        is ComposerAction.DraftChanged -> current.copy(draft = action.text)
        is ComposerAction.SubmitStarted -> current.copy(submitted = action.submission)
        is ComposerAction.SubmissionRejected -> {
            if (current.submitted?.requestId != action.requestId) {
                current
            } else {
                current.copy(submitted = null)
            }
        }
        is ComposerAction.SubmissionAcknowledged -> {
            val submitted = current.submitted
            if (submitted?.requestId != action.requestId) {
                current
            } else {
                current.copy(
                    draft = if (current.draft == submitted.text) "" else current.draft,
                    submitted = null,
                    lastAcknowledgedRequestId = action.requestId,
                )
            }
        }
    }
}
