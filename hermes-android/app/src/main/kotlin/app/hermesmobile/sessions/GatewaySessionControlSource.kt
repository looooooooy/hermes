package app.hermesmobile.sessions

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.ScopedWebSocketTicketRequest
import app.hermesmobile.protocol.gateway.GatewayConnectionRole
import app.hermesmobile.protocol.gateway.GatewaySocketState
import app.hermesmobile.protocol.gateway.ApprovalRespondRequest
import app.hermesmobile.protocol.gateway.ClarifyRespondRequest
import app.hermesmobile.protocol.gateway.PendingInputRespondResponse
import app.hermesmobile.protocol.gateway.PromptSubmitRequest
import app.hermesmobile.protocol.gateway.PromptSubmitResponse
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyAnswer
import app.hermesmobile.protocol.gateway.SessionCommandStatus
import app.hermesmobile.protocol.gateway.SessionCommandStatusRequest
import app.hermesmobile.protocol.gateway.SessionControlAcquireRequest
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControlReleaseRequest
import app.hermesmobile.protocol.gateway.SessionControlReleaseResponse
import app.hermesmobile.protocol.gateway.SessionControlRenewRequest
import app.hermesmobile.protocol.gateway.SessionControlStatus
import app.hermesmobile.protocol.gateway.SessionControlStatusRequest
import app.hermesmobile.protocol.gateway.SessionControllerClient
import app.hermesmobile.protocol.gateway.SessionControllerConnection
import app.hermesmobile.protocol.gateway.SessionControllerObserver
import app.hermesmobile.protocol.gateway.SessionControllerResult
import app.hermesmobile.protocol.gateway.SessionInterruptRequest
import app.hermesmobile.protocol.gateway.SessionInterruptResponse
import app.hermesmobile.protocol.gateway.SessionSteerRequest
import app.hermesmobile.protocol.gateway.SessionSteerResponse
import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.ClientTurnId
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionProjection
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.withTimeoutOrNull

/** Opens a control-only WebSocket bound to one durable/live session identity. */
class GatewaySessionControlSource(
    private val endpoint: GatewayEndpoint,
    private val ticketSource: ScopedWebSocketTicketSource,
    private val controllerClient: SessionControllerClient,
    private val clientInstanceId: ClientInstanceId,
    private val readyTimeoutMillis: Long = DEFAULT_READY_TIMEOUT_MILLIS,
) : SessionControlSource {
    init {
        require(readyTimeoutMillis > 0) { "Control ready timeout must be positive." }
    }

    override suspend fun open(
        session: SessionProjection,
        runtimeSessionId: RuntimeSessionId,
    ): SessionControlOpenResult {
        val profile = session.profile?.takeIf(String::isNotBlank) ?: DEFAULT_PROFILE
        val ticketRequest = ScopedWebSocketTicketRequest(
            connectionRole = GatewayConnectionRole.CONTROL,
            clientInstanceId = clientInstanceId,
            sessionKey = session.sessionKey,
            profile = profile,
        )
        val ticket = when (val minted = ticketSource.mint(ticketRequest)) {
            is ScopedWebSocketTicketResult.Ready -> minted.ticket
            ScopedWebSocketTicketResult.AuthenticationRequired -> {
                return SessionControlOpenResult.AuthenticationRequired
            }
            is ScopedWebSocketTicketResult.Unavailable -> {
                return SessionControlOpenResult.Unavailable(minted.summary)
            }
        }

        val opened = CompletableDeferred<OpenOutcome>()
        val transportEvents = MutableSharedFlow<SessionControlTransportEvent>(
            replay = 1,
            extraBufferCapacity = 8,
        )
        var ownershipTransferred = false
        val connection = controllerClient.connect(
            endpoint = endpoint,
            ticket = ticket,
            observer = object : SessionControllerObserver {
                override fun onStateChanged(state: GatewaySocketState) {
                    when (state) {
                        GatewaySocketState.Ready -> {
                            transportEvents.tryEmit(SessionControlTransportEvent.Ready)
                            opened.complete(OpenOutcome.Ready)
                        }
                        is GatewaySocketState.Closed -> {
                            val reason = "Hermes control connection closed (${state.code})."
                            transportEvents.tryEmit(SessionControlTransportEvent.Closed(reason))
                            opened.complete(OpenOutcome.Unavailable(reason))
                        }
                        is GatewaySocketState.Failed -> {
                            transportEvents.tryEmit(SessionControlTransportEvent.Closed(state.summary))
                            opened.complete(OpenOutcome.Unavailable(state.summary))
                        }
                        GatewaySocketState.Connecting,
                        GatewaySocketState.Open,
                        -> Unit
                    }
                }

                override fun onProtocolError() {
                    val reason = "Hermes server does not support the required control contract."
                    transportEvents.tryEmit(SessionControlTransportEvent.Closed(reason))
                    opened.complete(OpenOutcome.Unavailable(reason))
                }
            },
        )
        try {
            return when (
                val outcome = withTimeoutOrNull(readyTimeoutMillis) { opened.await() }
                    ?: OpenOutcome.Unavailable("Hermes control connection timed out.")
            ) {
                OpenOutcome.Ready -> {
                    val channel = GatewaySessionControlChannel(
                        connection = connection,
                        events = transportEvents,
                        session = session,
                        profile = profile,
                        runtimeSessionId = runtimeSessionId,
                        clientInstanceId = clientInstanceId,
                    )
                    ownershipTransferred = true
                    SessionControlOpenResult.Ready(channel)
                }
                is OpenOutcome.Unavailable -> {
                    SessionControlOpenResult.Unavailable(outcome.summary)
                }
            }
        } finally {
            if (!ownershipTransferred) {
                connection.close()
            }
        }
    }

    private sealed interface OpenOutcome {
        data object Ready : OpenOutcome
        data class Unavailable(val summary: String) : OpenOutcome
    }

    private companion object {
        const val DEFAULT_PROFILE = "default"
        const val DEFAULT_READY_TIMEOUT_MILLIS = 5_000L
    }
}

private class GatewaySessionControlChannel(
    private val connection: SessionControllerConnection,
    override val events: Flow<SessionControlTransportEvent>,
    private val session: SessionProjection,
    private val profile: String,
    private val runtimeSessionId: RuntimeSessionId,
    private val clientInstanceId: ClientInstanceId,
) : SessionControlChannel {
    override val availableMethods: Set<String> =
        requireNotNull(connection.ready).availableMethods

    override suspend fun acquire(): SessionControllerResult<SessionControlLease> =
        connection.acquire(
            SessionControlAcquireRequest(
                sessionKey = session.sessionKey,
                profile = profile,
                runtimeSessionId = runtimeSessionId,
                clientInstanceId = clientInstanceId,
            ),
        )

    override suspend fun status(): SessionControllerResult<SessionControlStatus> =
        connection.status(
            SessionControlStatusRequest(
                sessionKey = session.sessionKey,
                runtimeSessionId = runtimeSessionId,
                clientInstanceId = clientInstanceId,
                profile = profile,
            ),
        )

    override suspend fun renew(
        leaseId: SessionControlLeaseId,
    ): SessionControllerResult<SessionControlLease> = connection.renew(
        SessionControlRenewRequest(
            sessionKey = session.sessionKey,
            leaseId = leaseId,
            runtimeSessionId = runtimeSessionId,
            clientInstanceId = clientInstanceId,
            profile = profile,
        ),
    )

    override suspend fun release(
        leaseId: SessionControlLeaseId,
    ): SessionControllerResult<SessionControlReleaseResponse> = connection.release(
        SessionControlReleaseRequest(
            sessionKey = session.sessionKey,
            leaseId = leaseId,
            runtimeSessionId = runtimeSessionId,
            clientInstanceId = clientInstanceId,
            profile = profile,
        ),
    )

    override suspend fun commandStatus(
        method: String,
        requestId: ClientRequestId,
    ): SessionControllerResult<SessionCommandStatus> = connection.commandStatus(
        SessionCommandStatusRequest(
            sessionKey = session.sessionKey,
            method = method,
            clientRequestId = requestId,
            runtimeSessionId = runtimeSessionId,
            profile = profile,
        ),
    )

    override suspend fun submitPrompt(
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
        clientTurnId: ClientTurnId,
        text: String,
    ): SessionControllerResult<PromptSubmitResponse> = connection.submitPrompt(
        PromptSubmitRequest(
            sessionKey = session.sessionKey,
            leaseId = leaseId,
            clientRequestId = requestId,
            clientTurnId = clientTurnId,
            text = text,
            runtimeSessionId = runtimeSessionId,
        ),
    )

    override suspend fun interrupt(
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
    ): SessionControllerResult<SessionInterruptResponse> = connection.interrupt(
        SessionInterruptRequest(
            sessionKey = session.sessionKey,
            leaseId = leaseId,
            clientRequestId = requestId,
            runtimeSessionId = runtimeSessionId,
        ),
    )

    override suspend fun steer(
        leaseId: SessionControlLeaseId,
        requestId: ClientRequestId,
        text: String,
    ): SessionControllerResult<SessionSteerResponse> = connection.steer(
        SessionSteerRequest(
            sessionKey = session.sessionKey,
            leaseId = leaseId,
            clientRequestId = requestId,
            text = text,
            runtimeSessionId = runtimeSessionId,
        ),
    )

    override suspend fun respondApproval(
        leaseId: SessionControlLeaseId,
        clientRequestId: ClientRequestId,
        requestId: String,
        choice: SessionApprovalChoice,
    ): SessionControllerResult<PendingInputRespondResponse> = connection.respondApproval(
        ApprovalRespondRequest(
            sessionKey = session.sessionKey,
            runtimeSessionId = runtimeSessionId,
            leaseId = leaseId,
            clientRequestId = clientRequestId,
            requestId = requestId,
            choice = choice,
        ),
    )

    override suspend fun respondClarify(
        leaseId: SessionControlLeaseId,
        clientRequestId: ClientRequestId,
        requestId: String,
        answer: SessionClarifyAnswer,
    ): SessionControllerResult<PendingInputRespondResponse> = connection.respondClarify(
        ClarifyRespondRequest(
            scope = ClarifyRespondRequest.Scope(
                sessionKey = session.sessionKey,
                runtimeSessionId = runtimeSessionId,
                leaseId = leaseId,
                requestId = requestId,
            ),
            clientRequestId = clientRequestId,
            answer = answer,
        ),
    )

    override fun close() {
        connection.close()
    }
}
