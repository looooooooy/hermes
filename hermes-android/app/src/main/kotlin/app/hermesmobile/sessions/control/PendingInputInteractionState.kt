package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.ClientRequestId

data class PendingInputInteractionState(
    val requestId: String? = null,
    val selectedChoiceId: String? = null,
    val otherDraft: String = "",
    val requiresConfirmation: Boolean = false,
    val inFlightClientRequestId: ClientRequestId? = null,
    val outcome: PendingInputInteractionOutcome? = null,
) {
    val canSubmit: Boolean
        get() = requestId != null &&
            !requiresConfirmation &&
            (
                inFlightClientRequestId == null ||
                    outcome == PendingInputInteractionOutcome.RetryAvailable
                ) &&
            outcome != PendingInputInteractionOutcome.DeliveryUnknown &&
            (selectedChoiceId != null || otherDraft.isNotBlank())
}

sealed interface PendingInputInteractionOutcome {
    data object Accepted : PendingInputInteractionOutcome
    data object DeliveryUnknown : PendingInputInteractionOutcome
    data object RetryAvailable : PendingInputInteractionOutcome
    data object ResolvedElsewhere : PendingInputInteractionOutcome
    data class Failed(val summary: String) : PendingInputInteractionOutcome
}

sealed interface PendingInputInteractionAction {
    data class Observed(val requestId: String?) : PendingInputInteractionAction
    data class ChoiceSelected(
        val choiceId: String,
        val requiresConfirmation: Boolean,
    ) : PendingInputInteractionAction
    data class OtherDraftChanged(val text: String) : PendingInputInteractionAction
    data object ConfirmationGranted : PendingInputInteractionAction
    data object ConfirmationCancelled : PendingInputInteractionAction
    data class SubmissionStarted(val clientRequestId: ClientRequestId) : PendingInputInteractionAction
    data class Accepted(val clientRequestId: ClientRequestId) : PendingInputInteractionAction
    data class DeliveryUnknown(val clientRequestId: ClientRequestId) : PendingInputInteractionAction
    data class Failed(
        val clientRequestId: ClientRequestId,
        val summary: String,
    ) : PendingInputInteractionAction
    data class ReconciliationRequired(
        val clientRequestId: ClientRequestId,
        val summary: String,
        val clearAnswer: Boolean,
    ) : PendingInputInteractionAction
    data class ResolvedElsewhere(
        val clientRequestId: ClientRequestId,
    ) : PendingInputInteractionAction
}

class PendingInputInteractionReducer {
    fun reduce(
        current: PendingInputInteractionState,
        action: PendingInputInteractionAction,
    ): PendingInputInteractionState = when (action) {
        is PendingInputInteractionAction.Observed -> when (action.requestId) {
            current.requestId -> when {
                current.outcome == PendingInputInteractionOutcome.DeliveryUnknown &&
                    current.inFlightClientRequestId != null ->
                    current.copy(outcome = PendingInputInteractionOutcome.RetryAvailable)
                current.outcome is PendingInputInteractionOutcome.Failed &&
                    current.inFlightClientRequestId != null ->
                    current.copy(inFlightClientRequestId = null)
                else -> current
            }
            null -> PendingInputInteractionState(outcome = current.outcome)
            else -> PendingInputInteractionState(requestId = action.requestId)
        }

        is PendingInputInteractionAction.ChoiceSelected -> current.copy(
            selectedChoiceId = action.choiceId,
            requiresConfirmation = action.requiresConfirmation,
            outcome = null,
        )

        is PendingInputInteractionAction.OtherDraftChanged -> current.copy(
            selectedChoiceId = null,
            otherDraft = action.text,
            requiresConfirmation = false,
            outcome = null,
        )

        PendingInputInteractionAction.ConfirmationGranted -> if (current.requiresConfirmation) {
            current.copy(requiresConfirmation = false)
        } else {
            current
        }

        PendingInputInteractionAction.ConfirmationCancelled -> if (current.requiresConfirmation) {
            current.copy(
                selectedChoiceId = null,
                requiresConfirmation = false,
            )
        } else {
            current
        }

        is PendingInputInteractionAction.SubmissionStarted -> when {
            current.inFlightClientRequestId == null -> current.copy(
                inFlightClientRequestId = action.clientRequestId,
                outcome = null,
            )
            current.outcome == PendingInputInteractionOutcome.RetryAvailable &&
                current.inFlightClientRequestId == action.clientRequestId -> current.copy(outcome = null)
            else -> current
        }

        is PendingInputInteractionAction.Accepted -> current.reduceMatching(action.clientRequestId) {
            PendingInputInteractionState(outcome = PendingInputInteractionOutcome.Accepted)
        }

        is PendingInputInteractionAction.DeliveryUnknown ->
            current.reduceMatching(action.clientRequestId) {
                copy(outcome = PendingInputInteractionOutcome.DeliveryUnknown)
            }

        is PendingInputInteractionAction.Failed -> current.reduceMatching(action.clientRequestId) {
            copy(
                inFlightClientRequestId = null,
                outcome = PendingInputInteractionOutcome.Failed(action.summary),
            )
        }

        is PendingInputInteractionAction.ReconciliationRequired ->
            current.reduceMatching(action.clientRequestId) {
                copy(
                    selectedChoiceId = if (action.clearAnswer) null else selectedChoiceId,
                    otherDraft = if (action.clearAnswer) "" else otherDraft,
                    requiresConfirmation = if (action.clearAnswer) false else requiresConfirmation,
                    outcome = PendingInputInteractionOutcome.Failed(action.summary),
                )
            }

        is PendingInputInteractionAction.ResolvedElsewhere ->
            current.reduceMatching(action.clientRequestId) {
                PendingInputInteractionState(
                    outcome = PendingInputInteractionOutcome.ResolvedElsewhere,
                )
            }
    }

    private inline fun PendingInputInteractionState.reduceMatching(
        clientRequestId: ClientRequestId,
        transform: PendingInputInteractionState.() -> PendingInputInteractionState,
    ): PendingInputInteractionState = if (inFlightClientRequestId == clientRequestId) {
        transform()
    } else {
        this
    }
}
