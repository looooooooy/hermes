package app.hermesmobile.sessions.control

import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.ServerTurnId
import app.hermesmobile.protocol.gateway.SessionCommandState
import app.hermesmobile.protocol.gateway.SessionCommandStatus

data class CommandState(
    val commands: Map<ClientRequestId, CommandRecord> = emptyMap(),
)

data class CommandRecord(
    val requestId: ClientRequestId,
    val clientTurnId: ClientTurnId?,
    val serverTurnId: ServerTurnId? = null,
    val phase: CommandPhase,
    val promptPreview: String? = null,
    val failureSummary: String? = null,
) {
    val canAutomaticallyRetry: Boolean
        get() = false
}

enum class CommandPhase {
    SENDING,
    ACCEPTED,
    QUEUED,
    REJECTED,
    UNKNOWN,
    FAILED,
    COMPLETE,
}

sealed interface CommandAction {
    data class Started(
        val requestId: ClientRequestId,
        val clientTurnId: ClientTurnId? = null,
        val promptPreview: String? = null,
    ) : CommandAction

    data class DeliveryUnknown(val requestId: ClientRequestId) : CommandAction
    data class Failed(val requestId: ClientRequestId, val summary: String) : CommandAction
    data class Acknowledged(val status: SessionCommandStatus) : CommandAction
    data class StatusReconciled(val status: SessionCommandStatus) : CommandAction
}

class CommandStateReducer {
    fun reduce(current: CommandState, action: CommandAction): CommandState = when (action) {
        is CommandAction.Started -> current.withCommand(
            CommandRecord(
                requestId = action.requestId,
                clientTurnId = action.clientTurnId,
                phase = CommandPhase.SENDING,
                promptPreview = action.promptPreview,
            ),
        )
        is CommandAction.DeliveryUnknown -> current.update(action.requestId) {
            it.copy(phase = CommandPhase.UNKNOWN, failureSummary = null)
        }
        is CommandAction.Failed -> current.update(action.requestId) {
            it.copy(phase = CommandPhase.FAILED, failureSummary = action.summary)
        }
        is CommandAction.Acknowledged -> current.withStatus(action.status)
        is CommandAction.StatusReconciled -> current.withStatus(action.status)
    }

    private fun CommandState.withCommand(command: CommandRecord): CommandState =
        copy(commands = commands + (command.requestId to command))

    private fun CommandState.withStatus(status: SessionCommandStatus): CommandState {
        val prior = commands[status.clientRequestId]
        return withCommand(
            CommandRecord(
                requestId = status.clientRequestId,
                clientTurnId = status.clientTurnId ?: prior?.clientTurnId,
                serverTurnId = status.serverTurnId ?: prior?.serverTurnId,
                phase = status.status.toPhase(),
                promptPreview = prior?.promptPreview,
            ),
        )
    }

    private inline fun CommandState.update(
        requestId: ClientRequestId,
        transform: (CommandRecord) -> CommandRecord,
    ): CommandState {
        val command = commands[requestId] ?: return this
        return withCommand(transform(command))
    }

    private fun SessionCommandState.toPhase(): CommandPhase = when (this) {
        SessionCommandState.ACCEPTED -> CommandPhase.ACCEPTED
        SessionCommandState.QUEUED -> CommandPhase.QUEUED
        SessionCommandState.REJECTED -> CommandPhase.REJECTED
        SessionCommandState.UNKNOWN -> CommandPhase.UNKNOWN
    }
}
