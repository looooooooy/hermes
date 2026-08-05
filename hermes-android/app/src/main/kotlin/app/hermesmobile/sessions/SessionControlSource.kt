package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.gateway.PendingInputRespondResponse
import app.hermesmobile.protocol.gateway.PromptSubmitResponse
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyAnswer
import app.hermesmobile.protocol.gateway.SessionCommandStatus
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControlReleaseResponse
import app.hermesmobile.protocol.gateway.SessionControlStatus
import app.hermesmobile.protocol.gateway.SessionControllerResult
import app.hermesmobile.protocol.gateway.SessionInterruptResponse
import app.hermesmobile.protocol.gateway.SessionSteerResponse
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionProjection
import kotlinx.coroutines.flow.Flow

sealed interface SessionControlOpenResult {
    data class Ready(val channel: SessionControlChannel) : SessionControlOpenResult
    data object AuthenticationRequired : SessionControlOpenResult
    data class Unavailable(val summary: String) : SessionControlOpenResult
}

fun interface SessionControlSource {
    suspend fun open(
        session: SessionProjection,
        runtimeSessionId: RuntimeSessionId,
    ): SessionControlOpenResult
}

sealed interface SessionControlTransportEvent {
    data object Ready : SessionControlTransportEvent
    data class Closed(val reason: String?) : SessionControlTransportEvent
}

interface SessionControlChannel {
    val events: Flow<SessionControlTransportEvent>
    val availableMethods: Set<String>

    suspend fun acquire(): SessionControllerResult<SessionControlLease>

    suspend fun status(): SessionControllerResult<SessionControlStatus>

    suspend fun renew(
        leaseId: SessionControlLeaseId,
    ): SessionControllerResult<SessionControlLease>

    suspend fun release(
        leaseId: SessionControlLeaseId,
    ): SessionControllerResult<SessionControlReleaseResponse>

    suspend fun commandStatus(
        method: String,
        requestId: ClientRequestId,
    ): SessionControllerResult<SessionCommandStatus>

    suspend fun submitPrompt(
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
        clientTurnId: ClientTurnId,
        text: String,
    ): SessionControllerResult<PromptSubmitResponse>

    suspend fun interrupt(
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
    ): SessionControllerResult<SessionInterruptResponse>

    suspend fun steer(
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
        text: String,
    ): SessionControllerResult<SessionSteerResponse>

    suspend fun respondApproval(
        leaseId: SessionControlLeaseId,
        clientRequestId: ClientRequestId,
        requestId: String,
        choice: SessionApprovalChoice,
    ): SessionControllerResult<PendingInputRespondResponse>

    suspend fun respondClarify(
        leaseId: SessionControlLeaseId,
        clientRequestId: ClientRequestId,
        requestId: String,
        answer: SessionClarifyAnswer,
    ): SessionControllerResult<PendingInputRespondResponse>

    fun close()
}
