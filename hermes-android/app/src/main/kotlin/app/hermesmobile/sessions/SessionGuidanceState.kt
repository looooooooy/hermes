package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.ClientRequestId

enum class SessionGuidancePhase {
    IDLE,
    SUBMITTING,
    ACCEPTED,
    DELIVERY_UNKNOWN,
    FAILED,
}

data class SessionGuidanceState(
    val draft: String = "",
    val phase: SessionGuidancePhase = SessionGuidancePhase.IDLE,
    val inFlightRequestId: ClientRequestId? = null,
    val submittedText: String? = null,
    val failureSummary: String? = null,
) {
    fun withDraft(text: String): SessionGuidanceState = copy(
        draft = text,
        phase = if (inFlightRequestId == null) SessionGuidancePhase.IDLE else phase,
        failureSummary = null,
    )

    fun started(requestId: ClientRequestId, text: String): SessionGuidanceState = copy(
        phase = SessionGuidancePhase.SUBMITTING,
        inFlightRequestId = requestId,
        submittedText = text,
        failureSummary = null,
    )

    fun accepted(requestId: ClientRequestId): SessionGuidanceState {
        if (requestId != inFlightRequestId) return this
        return copy(
            draft = if (draft == submittedText) "" else draft,
            phase = SessionGuidancePhase.ACCEPTED,
            inFlightRequestId = null,
            submittedText = null,
            failureSummary = null,
        )
    }

    fun deliveryUnknown(requestId: ClientRequestId): SessionGuidanceState {
        if (requestId != inFlightRequestId) return this
        return copy(phase = SessionGuidancePhase.DELIVERY_UNKNOWN)
    }

    fun failed(requestId: ClientRequestId, summary: String): SessionGuidanceState {
        if (requestId != inFlightRequestId) return this
        return copy(
            phase = SessionGuidancePhase.FAILED,
            inFlightRequestId = null,
            submittedText = null,
            failureSummary = summary,
        )
    }
}
