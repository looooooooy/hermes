package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.ControlLossReason
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome

internal enum class SessionChromeBadge {
    Controller,
    Observer,
    Restored,
    Acquiring,
    ControlUnavailable,
    Disconnected,
}

internal data class SessionChromePresentation(
    val badge: SessionChromeBadge,
    val showsStatusStrip: Boolean,
    val showsControlLossReason: Boolean,
    val canRetryControl: Boolean,
)

internal enum class CurrentExecutionKind {
    TODO,
    THINKING,
    TOOL,
    SUBAGENT,
    RESPONSE,
    ACTIVITY,
    WORKING,
}

internal data class CurrentExecutionPresentation(
    val kind: CurrentExecutionKind,
    val detail: String?,
)

internal fun controllerOwnerDisplayLabel(mode: ControlMode.Conflict): String =
    mode.controllerLabel?.takeIf(String::isNotBlank) ?: when (mode.controllerKind) {
        SessionControllerKind.DESKTOP -> "Hermes Desktop"
        SessionControllerKind.MOBILE -> "Hermes Mobile"
        SessionControllerKind.NONE -> "Hermes"
    }

internal fun sessionChromePresentation(
    controlMode: ControlMode,
    connectionStatus: RealtimeConnectionStatus,
    pendingOutcome: PendingInputInteractionOutcome?,
    hasStatusMessage: Boolean,
    requiresServerUpgrade: Boolean = false,
    hasControlCapability: Boolean = true,
): SessionChromePresentation {
    val badge = when {
        connectionStatus == RealtimeConnectionStatus.DISCONNECTED ->
            SessionChromeBadge.Disconnected
        pendingOutcome == PendingInputInteractionOutcome.RetryAvailable &&
            controlMode is ControlMode.Controller -> SessionChromeBadge.Restored
        controlMode is ControlMode.Controller -> SessionChromeBadge.Controller
        controlMode == ControlMode.Acquiring -> SessionChromeBadge.Acquiring
        controlMode is ControlMode.Lost || controlMode is ControlMode.Conflict ->
            SessionChromeBadge.ControlUnavailable
        else -> SessionChromeBadge.Observer
    }
    val connectionNeedsAttention = connectionStatus !in setOf(
        RealtimeConnectionStatus.IDLE,
        RealtimeConnectionStatus.LIVE,
    )
    val liveObserver = badge == SessionChromeBadge.Observer &&
        connectionStatus == RealtimeConnectionStatus.LIVE
    val controlLossReason = (controlMode as? ControlMode.Lost)?.reason
    val capabilityUnavailable = connectionStatus == RealtimeConnectionStatus.LIVE &&
        !hasControlCapability &&
        controlLossReason != ControlLossReason.LEASE_EXPIRED
    return SessionChromePresentation(
        badge = badge,
        showsStatusStrip = badge in setOf(
            SessionChromeBadge.Restored,
            SessionChromeBadge.Acquiring,
            SessionChromeBadge.ControlUnavailable,
            SessionChromeBadge.Disconnected,
        ) || liveObserver || connectionNeedsAttention || hasStatusMessage || requiresServerUpgrade,
        showsControlLossReason =
            badge == SessionChromeBadge.ControlUnavailable && !capabilityUnavailable,
        canRetryControl = when (controlLossReason) {
            ControlLossReason.LEASE_EXPIRED -> true
            ControlLossReason.CONNECTION_LOST -> !capabilityUnavailable
            ControlLossReason.RELEASED,
            ControlLossReason.REJECTED,
            null,
            -> false
        },
    )
}

internal fun currentExecutionPresentation(
    running: Boolean,
    turns: List<ConversationTurnUiModel>,
): CurrentExecutionPresentation? {
    if (!running) return null
    return turns.asReversed().firstNotNullOfOrNull { turn ->
        turn.renderSections().asReversed().firstNotNullOfOrNull { section ->
            section.currentExecutionPresentation()
        }
    } ?: CurrentExecutionPresentation(CurrentExecutionKind.WORKING, detail = null)
}

private fun HermesConversationSection.currentExecutionPresentation(): CurrentExecutionPresentation? {
    if (status !in ACTIVE_EXECUTION_STATUSES) return null
    return when (this) {
        is HermesConversationSection.Todo -> items
            .firstOrNull { it.status == HermesConversationTodoStatus.IN_PROGRESS }
            ?.content
            ?.takeIf(String::isNotBlank)
            ?.let { CurrentExecutionPresentation(CurrentExecutionKind.TODO, it) }
            ?: CurrentExecutionPresentation(CurrentExecutionKind.TODO, detail = null)

        is HermesConversationSection.Thinking ->
            CurrentExecutionPresentation(CurrentExecutionKind.THINKING, detail = null)

        is HermesConversationSection.ToolGroup -> tools
            .asReversed()
            .firstOrNull { it.status == ConversationToolStatus.RUNNING }
            ?.let { tool ->
                CurrentExecutionPresentation(
                    kind = CurrentExecutionKind.TOOL,
                    detail = tool.callLabel
                        ?.takeIf(String::isNotBlank)
                        ?: tool.name?.takeIf(String::isNotBlank),
                )
            }
            ?: CurrentExecutionPresentation(CurrentExecutionKind.TOOL, detail = null)

        is HermesConversationSection.Subagents -> subagents
            .asReversed()
            .firstOrNull { it.status in ACTIVE_EXECUTION_STATUSES }
            ?.goal
            ?.takeIf(String::isNotBlank)
            ?.let { CurrentExecutionPresentation(CurrentExecutionKind.SUBAGENT, it) }
            ?: CurrentExecutionPresentation(CurrentExecutionKind.SUBAGENT, detail = null)

        is HermesConversationSection.AssistantResponse ->
            CurrentExecutionPresentation(CurrentExecutionKind.RESPONSE, detail = null)

        is HermesConversationSection.Activity -> CurrentExecutionPresentation(
            kind = CurrentExecutionKind.ACTIVITY,
            detail = text.takeIf(String::isNotBlank),
        )

        is HermesConversationSection.UserPrompt,
        is HermesConversationSection.ResponseBoundary,
        is HermesConversationSection.Event,
        is HermesConversationSection.Diff,
        is HermesConversationSection.Error,
        is HermesConversationSection.TokenSummary,
        is HermesConversationSection.PendingInput,
        -> null
    }
}

private val ACTIVE_EXECUTION_STATUSES = setOf(
    HermesConversationSectionStatus.RUNNING,
    HermesConversationSectionStatus.STREAMING,
)
