package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.sessions.control.CommandPhase
import app.hermesmobile.sessions.control.CommandState
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.ControlState
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import app.hermesmobile.sessions.control.PendingInputInteractionState

internal enum class PendingInputDockMode {
    Approval,
    Clarify,
    Restored,
}

internal data class PendingInputDockPresentation(
    val mode: PendingInputDockMode,
    val selectedChoiceId: String?,
    val editingEnabled: Boolean,
    val showsChoices: Boolean,
    val showsConfirmation: Boolean,
    val submitEnabled: Boolean,
    val retryEnabled: Boolean,
)

internal const val PENDING_INPUT_MIN_TOUCH_TARGET_DP = 48
internal const val QUEUED_PROMPT_WINDOW_SIZE = 3

internal enum class SessionBottomInputSurface {
    Composer,
    Decision,
}

internal fun sessionBottomInputSurface(
    pendingInput: SessionPendingInput?,
): SessionBottomInputSurface = if (pendingInput == null) {
    SessionBottomInputSurface.Composer
} else {
    SessionBottomInputSurface.Decision
}

internal fun authoritativePendingInput(control: ControlState): SessionPendingInput? =
    (control.mode as? ControlMode.Controller)?.lease?.pendingInput

internal fun sessionComposerGuidanceActionVisible(
    running: Boolean,
    canMutate: Boolean,
    isInterrupting: Boolean,
): Boolean = running && canMutate && !isInterrupting

internal data class QueuedPromptPresentation(
    val requestId: ClientRequestId,
    val preview: String,
)

internal data class QueuedPromptWindow(
    val totalCount: Int,
    val hiddenBeforeCount: Int,
    val items: List<QueuedPromptPresentation>,
)

internal fun queuedPromptWindow(commands: CommandState): QueuedPromptWindow {
    val queued = commands.commands.values.mapNotNull { command ->
        command.promptPreview
            ?.takeIf(String::isNotBlank)
            ?.takeIf { command.phase == CommandPhase.QUEUED }
            ?.let { preview ->
                QueuedPromptPresentation(
                    requestId = command.requestId,
                    preview = preview,
                )
            }
    }
    val items = queued.takeLast(QUEUED_PROMPT_WINDOW_SIZE)
    return QueuedPromptWindow(
        totalCount = queued.size,
        hiddenBeforeCount = queued.size - items.size,
        items = items,
    )
}

internal data class ApprovalChoicePresentation(
    val requiresConfirmation: Boolean,
    val submitsImmediately: Boolean,
)

internal fun approvalChoicePresentation(
    choice: SessionApprovalChoice,
): ApprovalChoicePresentation {
    val requiresConfirmation = choice == SessionApprovalChoice.ALLOW_ALWAYS
    return ApprovalChoicePresentation(
        requiresConfirmation = requiresConfirmation,
        submitsImmediately = !requiresConfirmation,
    )
}

internal enum class PendingInputFeedbackAnnouncement {
    Polite,
    Assertive,
}

internal data class PendingInputFeedbackSemantics(
    val announcement: PendingInputFeedbackAnnouncement,
    val isError: Boolean,
)

internal fun pendingInputFeedbackSemantics(
    outcome: PendingInputInteractionOutcome?,
): PendingInputFeedbackSemantics? = when (outcome) {
    is PendingInputInteractionOutcome.Failed -> PendingInputFeedbackSemantics(
        announcement = PendingInputFeedbackAnnouncement.Assertive,
        isError = true,
    )
    PendingInputInteractionOutcome.DeliveryUnknown,
    PendingInputInteractionOutcome.RetryAvailable,
    PendingInputInteractionOutcome.ResolvedElsewhere,
    -> PendingInputFeedbackSemantics(
        announcement = PendingInputFeedbackAnnouncement.Polite,
        isError = false,
    )
    null,
    PendingInputInteractionOutcome.Accepted,
    -> null
}

internal fun pendingInputDockPresentation(
    pendingInput: SessionPendingInput,
    interaction: PendingInputInteractionState,
    mutationEnabled: Boolean = true,
): PendingInputDockPresentation {
    val restored = interaction.outcome == PendingInputInteractionOutcome.RetryAvailable
    val confirmation = pendingInput is SessionPendingInput.Approval &&
        interaction.requiresConfirmation
    val requestIdentityMatches = interaction.requestId == pendingInput.requestId
    val editingEnabled = mutationEnabled &&
        requestIdentityMatches &&
        !restored &&
        interaction.inFlightClientRequestId == null
    return PendingInputDockPresentation(
        mode = when {
            restored -> PendingInputDockMode.Restored
            pendingInput is SessionPendingInput.Approval -> PendingInputDockMode.Approval
            else -> PendingInputDockMode.Clarify
        },
        selectedChoiceId = interaction.selectedChoiceId,
        editingEnabled = editingEnabled,
        showsChoices = !confirmation,
        showsConfirmation = !restored && confirmation,
        submitEnabled = pendingInput is SessionPendingInput.Clarify &&
            editingEnabled &&
            interaction.canSubmit,
        retryEnabled = mutationEnabled &&
            restored &&
            requestIdentityMatches &&
            interaction.canSubmit,
    )
}

internal data class TranscriptComposerPresentation(
    val primaryAction: TranscriptComposerPrimaryAction,
    val inputEnabled: Boolean,
    val primaryEnabled: Boolean,
    val keyboardSendEnabled: Boolean,
    val stopActionVisible: Boolean,
    val stopEnabled: Boolean,
)

internal fun transcriptComposerPresentation(
    running: Boolean,
    isInterrupting: Boolean,
    guidanceMode: Boolean = false,
    canEdit: Boolean,
    canSend: Boolean,
    canStop: Boolean,
    hasDraft: Boolean,
): TranscriptComposerPresentation {
    val action = transcriptComposerPrimaryAction(
        running = running,
        isInterrupting = isInterrupting,
        guidanceMode = guidanceMode,
    )
    val sendEnabled = canSend && hasDraft && !isInterrupting
    return TranscriptComposerPresentation(
        primaryAction = action,
        inputEnabled = canEdit,
        primaryEnabled = when (action) {
            TranscriptComposerPrimaryAction.Send -> sendEnabled
            TranscriptComposerPrimaryAction.Queue -> sendEnabled
            TranscriptComposerPrimaryAction.Guide -> sendEnabled
        },
        keyboardSendEnabled = sendEnabled,
        stopActionVisible = running || isInterrupting,
        stopEnabled = canStop,
    )
}
