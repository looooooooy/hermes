package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.ScopedWebSocketTicket
import app.hermesmobile.protocol.auth.WebSocketTicket
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

object MobileControlMethods {
    const val ACQUIRE = "session.control.acquire"
    const val RENEW = "session.control.renew"
    const val RELEASE = "session.control.release"
    const val STATUS = "session.control.status"
    const val COMMAND_STATUS = "session.command.status"
    const val PROMPT_SUBMIT = "prompt.submit"
    const val SESSION_INTERRUPT = "session.interrupt"
    const val SESSION_STEER = "session.steer"
    const val APPROVAL_RESPOND = "approval.respond"
    const val CLARIFY_RESPOND = "clarify.respond"

    const val SESSION_REDIRECT = "session.redirect"
    const val SUDO_RESPOND = "sudo.respond"
    const val SECRET_RESPOND = "secret.respond"
    const val TERMINAL_READ_RESPOND = "terminal.read.respond"

    val IMPLEMENTED: Set<String> = setOf(
        ACQUIRE,
        RENEW,
        RELEASE,
        STATUS,
        COMMAND_STATUS,
        PROMPT_SUBMIT,
        SESSION_INTERRUPT,
        SESSION_STEER,
        APPROVAL_RESPOND,
        CLARIFY_RESPOND,
    )

    val COMMAND_STATUS_METHODS: Set<String> = setOf(
        PROMPT_SUBMIT,
        SESSION_INTERRUPT,
        SESSION_STEER,
        APPROVAL_RESPOND,
        CLARIFY_RESPOND,
    )

    val FROZEN: Set<String> = IMPLEMENTED + setOf(
        SESSION_REDIRECT,
        SUDO_RESPOND,
        SECRET_RESPOND,
        TERMINAL_READ_RESPOND,
    )
}

object MobileControlErrorCodes {
    val EXPECTED: Map<String, Int> = mapOf(
        "control_role_required" to 4200,
        "control_contract_unsupported" to 4201,
        "live_runtime_unavailable" to 4202,
        "controller_conflict" to 4203,
        "lease_required" to 4204,
        "lease_expired" to 4205,
        "lease_mismatch" to 4206,
        "request_id_payload_conflict" to 4207,
        "pending_request_conflict" to 4208,
        "method_not_allowed" to 4209,
        "command_unknown" to 4210,
        "revision_conflict" to 4211,
        "session_binding_mismatch" to 4212,
        "invalid_pending_response" to 4213,
        "owner_adapter_unavailable" to 4214,
        "relay_overloaded" to 4215,
        "deadline_exceeded_before_effect" to 4306,
        "effect_unknown" to 4307,
    )

    fun isValidAdvertisement(errorCodes: Map<String, Int>): Boolean =
        errorCodes == EXPECTED
}

data class SessionControlReady(
    val controlContractVersion: Int,
    val connectionRole: GatewayConnectionRole,
    val availableMethods: Set<String>,
    val errorCodes: Map<String, Int>,
) {
    fun supports(method: String): Boolean = method in availableMethods
}

enum class SessionControllerKind(val wireValue: String) {
    DESKTOP("desktop"),
    MOBILE("mobile"),
    NONE("none"),
    ;

    companion object {
        fun fromWireValue(value: String?): SessionControllerKind? =
            entries.firstOrNull { it.wireValue == value }

        /** Legacy owner relays used `local`; Cloud v1 canonicalizes that owner as desktop. */
        fun normalizeStatusWireValue(value: String?): SessionControllerKind? = when (value) {
            "local" -> DESKTOP
            else -> fromWireValue(value)
        }
    }
}

data class SessionControlLeaseId(val value: String) {
    init {
        require(value.isNotBlank()) { "Controller lease id is required." }
    }

    override fun toString(): String = "SessionControlLeaseId(value=[REDACTED])"
}

@JvmInline
value class ClientRequestId(val value: String) {
    init {
        require(value.isNotBlank()) { "Client request id is required." }
    }
}

@JvmInline
value class ClientTurnId(val value: String) {
    init {
        require(value.isNotBlank()) { "Client turn id is required." }
    }
}

@JvmInline
value class ServerTurnId(val value: String) {
    init {
        require(value.isNotBlank()) { "Server turn id is required." }
    }
}

enum class SessionApprovalChoice(val wireValue: String) {
    ALLOW_ONCE("allow_once"),
    ALLOW_SESSION("allow_session"),
    ALLOW_ALWAYS("allow_always"),
    DENY("deny"),
    ;

    companion object {
        fun fromWireValue(value: String?): SessionApprovalChoice? =
            entries.firstOrNull { it.wireValue == value }
    }
}

data class SessionClarifyChoice(
    val id: String,
    val label: String,
) {
    init {
        require(id.isNotBlank()) { "Clarify choice id is required." }
        require(label.isNotBlank()) { "Clarify choice label is required." }
    }

    override fun toString(): String = "SessionClarifyChoice(id=[REDACTED], label=[REDACTED])"
}

sealed interface SessionPendingInput {
    val requestId: String
    val expiresAtEpochMs: Long

    data class Approval(
        override val requestId: String,
        val title: String,
        val description: String,
        val command: String,
        val choices: List<SessionApprovalChoice>,
        override val expiresAtEpochMs: Long,
    ) : SessionPendingInput {
        init {
            require(requestId.isNotBlank()) { "Pending request id is required." }
            require(title.isNotBlank()) { "Approval title is required." }
            require(choices.isNotEmpty()) { "Approval choices are required." }
            require(choices.distinct().size == choices.size) { "Approval choices must be unique." }
            require(expiresAtEpochMs >= 0) { "Pending request expiry must not be negative." }
        }

        override fun toString(): String =
            "SessionPendingInput.Approval(requestId=[REDACTED], choices=${choices.size}, " +
                "expiresAtEpochMs=$expiresAtEpochMs)"
    }

    data class Clarify(
        override val requestId: String,
        val question: String,
        val choices: List<SessionClarifyChoice>,
        val allowOther: Boolean,
        override val expiresAtEpochMs: Long,
    ) : SessionPendingInput {
        init {
            require(requestId.isNotBlank()) { "Pending request id is required." }
            require(question.isNotBlank()) { "Clarify question is required." }
            require(choices.map(SessionClarifyChoice::id).distinct().size == choices.size) {
                "Clarify choice ids must be unique."
            }
            require(choices.map(SessionClarifyChoice::label).distinct().size == choices.size) {
                "Clarify choice labels must be unique."
            }
            require(choices.isNotEmpty() || allowOther) { "Clarify must provide an answer form." }
            require(expiresAtEpochMs >= 0) { "Pending request expiry must not be negative." }
        }

        override fun toString(): String =
            "SessionPendingInput.Clarify(requestId=[REDACTED], choices=${choices.size}, " +
                "allowOther=$allowOther, expiresAtEpochMs=$expiresAtEpochMs)"
    }
}

data class SessionControlAcquireRequest(
    val sessionKey: SessionKey,
    val profile: String,
    val runtimeSessionId: RuntimeSessionId? = null,
    val clientInstanceId: ClientInstanceId,
) {
    init {
        require(profile.isNotBlank()) { "Profile must not be blank." }
    }
}

data class SessionControlRenewRequest(
    val sessionKey: SessionKey,
    val leaseId: SessionControlLeaseId,
    val runtimeSessionId: RuntimeSessionId? = null,
    val clientInstanceId: ClientInstanceId? = null,
    val profile: String? = null,
) {
    init {
        require(profile == null || profile.isNotBlank()) { "Profile must not be blank." }
    }
}

data class SessionControlStatusRequest(
    val sessionKey: SessionKey,
    val profile: String? = null,
    val runtimeSessionId: RuntimeSessionId? = null,
    val clientInstanceId: ClientInstanceId? = null,
) {
    init {
        require(profile == null || profile.isNotBlank()) { "Profile must not be blank." }
    }
}

data class SessionControlReleaseRequest(
    val sessionKey: SessionKey,
    val leaseId: SessionControlLeaseId,
    val runtimeSessionId: RuntimeSessionId? = null,
    val clientInstanceId: ClientInstanceId? = null,
    val profile: String? = null,
) {
    init {
        require(profile == null || profile.isNotBlank()) { "Profile must not be blank." }
    }
}

data class SessionControlLease(
    val leaseId: SessionControlLeaseId,
    val expiresAtEpochMs: Long,
    val controlRevision: Long,
    val controllerKind: SessionControllerKind,
    val controllerLabel: String,
    val pendingInput: SessionPendingInput?,
) {
    init {
        require(expiresAtEpochMs >= 0) { "Lease expiry must not be negative." }
        require(controlRevision >= 0) { "Control revision must not be negative." }
        require(controllerLabel.isNotBlank()) { "Controller label is required." }
    }

    override fun toString(): String =
        "SessionControlLease(leaseId=[REDACTED], expiresAtEpochMs=$expiresAtEpochMs, " +
            "controlRevision=$controlRevision, controllerKind=$controllerKind, " +
            "controllerLabel=$controllerLabel, pendingInput=${pendingInput != null})"
}

data class SessionControlStatus(
    val controllerKind: SessionControllerKind,
    val controllerLabel: String?,
    val controlRevision: Long,
    val leaseExpiresAtEpochMs: Long,
    val pendingInput: SessionPendingInput?,
) {
    init {
        require(
            if (controllerKind == SessionControllerKind.NONE) {
                controllerLabel == null
            } else {
                !controllerLabel.isNullOrBlank()
            },
        ) {
            "Controller label must be null only when no controller is active."
        }
        require(controlRevision >= 0) { "Control revision must not be negative." }
        require(leaseExpiresAtEpochMs >= 0) { "Lease expiry must not be negative." }
    }

    override fun toString(): String =
        "SessionControlStatus(controllerKind=$controllerKind, controllerLabel=$controllerLabel, " +
            "controlRevision=$controlRevision, leaseExpiresAtEpochMs=$leaseExpiresAtEpochMs, " +
            "pendingInput=${pendingInput != null})"
}

data class SessionControlReleaseResponse(
    val released: Boolean,
    val controlRevision: Long,
) {
    init {
        require(controlRevision >= 0) { "Control revision must not be negative." }
    }
}

enum class SessionCommandState(val wireValue: String) {
    ACCEPTED("accepted"),
    QUEUED("queued"),
    REJECTED("rejected"),
    UNKNOWN("unknown"),
    ;

    companion object {
        fun fromWireValue(value: String?): SessionCommandState? =
            entries.firstOrNull { it.wireValue == value }
    }
}

data class SessionCommandStatusRequest(
    val sessionKey: SessionKey,
    val method: String,
    val clientRequestId: ClientRequestId,
    val runtimeSessionId: RuntimeSessionId? = null,
    val profile: String? = null,
) {
    init {
        require(method in MobileControlMethods.COMMAND_STATUS_METHODS) {
            "Command status method must be an owner action."
        }
        require(profile == null || profile.isNotBlank()) { "Profile must not be blank." }
    }
}

data class SessionCommandStatus(
    val status: SessionCommandState,
    val clientRequestId: ClientRequestId,
    val clientTurnId: ClientTurnId? = null,
    val serverTurnId: ServerTurnId? = null,
)

data class PromptSubmitRequest(
    val sessionKey: SessionKey,
    val leaseId: SessionControlLeaseId,
    val clientRequestId: ClientRequestId,
    val clientTurnId: ClientTurnId,
    val text: String,
    val runtimeSessionId: RuntimeSessionId? = null,
) {
    init {
        require(text.isNotBlank()) { "Prompt text is required." }
    }
}

data class PromptSubmitResponse(
    val status: SessionCommandState,
    val clientRequestId: ClientRequestId,
    val clientTurnId: ClientTurnId,
    val serverTurnId: ServerTurnId? = null,
)

data class SessionInterruptRequest(
    val sessionKey: SessionKey,
    val leaseId: SessionControlLeaseId,
    val clientRequestId: ClientRequestId,
    val runtimeSessionId: RuntimeSessionId? = null,
)

data class SessionInterruptResponse(
    val status: SessionCommandState,
    val clientRequestId: ClientRequestId,
)

data class SessionSteerRequest(
    val sessionKey: SessionKey,
    val leaseId: SessionControlLeaseId,
    val clientRequestId: ClientRequestId,
    val text: String,
    val runtimeSessionId: RuntimeSessionId? = null,
) {
    init {
        require(text.isNotBlank()) { "Guidance text is required." }
    }
}

data class SessionSteerResponse(
    val status: SessionCommandState,
    val clientRequestId: ClientRequestId,
)

enum class PendingInputKind(val wireValue: String) {
    APPROVAL("approval"),
    CLARIFY("clarify"),
}

data class ApprovalRespondRequest(
    val sessionKey: SessionKey,
    val leaseId: SessionControlLeaseId,
    val clientRequestId: ClientRequestId,
    val requestId: String,
    val choice: SessionApprovalChoice,
    val runtimeSessionId: RuntimeSessionId? = null,
) {
    init {
        require(requestId.isNotBlank()) { "Pending request id is required." }
    }
}

sealed interface SessionClarifyAnswer {
    data class Choice(val choiceId: String) : SessionClarifyAnswer {
        init {
            require(choiceId.isNotBlank()) { "Clarify choice id is required." }
        }
    }

    data class Other(val text: String) : SessionClarifyAnswer {
        init {
            require(text.isNotBlank()) { "Clarify other text is required." }
        }
    }
}

data class ClarifyRespondRequest(
    val scope: Scope,
    val clientRequestId: ClientRequestId,
    val answer: SessionClarifyAnswer,
) {
    data class Scope(
        val sessionKey: SessionKey,
        val leaseId: SessionControlLeaseId,
        val requestId: String,
        val runtimeSessionId: RuntimeSessionId? = null,
    ) {
        init {
            require(requestId.isNotBlank()) { "Pending request id is required." }
        }
    }
}

data class PendingInputRespondResponse(
    val kind: PendingInputKind,
    val requestId: String,
    val clientRequestId: ClientRequestId,
    val controlRevision: Long,
) {
    init {
        require(requestId.isNotBlank()) { "Pending request id is required." }
        require(controlRevision >= 0) { "Control revision must not be negative." }
    }
}

sealed interface SessionControllerResult<out T> {
    data class Success<T>(val value: T) : SessionControllerResult<T>
    data object Unsupported : SessionControllerResult<Nothing>
    data object NotReady : SessionControllerResult<Nothing>
    data object Disconnected : SessionControllerResult<Nothing>
    data object Timeout : SessionControllerResult<Nothing>
    data class RpcFailure(val error: JsonRpcError) : SessionControllerResult<Nothing>
    data object InvalidResponse : SessionControllerResult<Nothing>
}

interface SessionControllerObserver {
    fun onStateChanged(state: GatewaySocketState)
    fun onEvent(event: GatewayEvent) = Unit
    fun onProtocolError() = Unit
}

class SessionControllerClient(
    private val gatewayClient: GatewayWebSocketClient,
) {
    fun connect(
        endpoint: GatewayEndpoint,
        ticket: ScopedWebSocketTicket,
        observer: SessionControllerObserver,
    ): SessionControllerConnection {
        require(ticket.connectionRole == GatewayConnectionRole.CONTROL) {
            "A control-scoped WebSocket ticket is required."
        }
        require(ticket.value.isNotBlank()) { "WebSocket ticket is required." }
        require(ticket.ttlSeconds > 0) { "WebSocket ticket TTL must be positive." }

        val bridge = SessionControllerObserverBridge(observer)
        val gatewayConnection = gatewayClient.connect(
            endpoint = endpoint,
            ticket = WebSocketTicket(ticket.value, ticket.ttlSeconds),
            observerContractVersion = 1,
            observer = bridge,
        )
        return SessionControllerConnection(gatewayConnection, observer).also(bridge::attach)
    }
}

class SessionControllerConnection internal constructor(
    private val gatewayConnection: GatewayConnection,
    private val observer: SessionControllerObserver,
) {
    @Volatile
    var ready: SessionControlReady? = null
        private set

    @Volatile
    private var handshakeRejected: Boolean = false

    val state: GatewaySocketState
        get() = gatewayConnection.state

    suspend fun acquire(
        request: SessionControlAcquireRequest,
    ): SessionControllerResult<SessionControlLease> = rpc(
        method = MobileControlMethods.ACQUIRE,
        params = buildJsonObject {
            put("session_key", request.sessionKey.value)
            put("profile", request.profile)
            request.runtimeSessionId?.let { put("runtime_session_id", it.value) }
            put("client_instance_id", request.clientInstanceId.value)
        },
        decode = ::decodeLease,
    )

    suspend fun renew(
        request: SessionControlRenewRequest,
    ): SessionControllerResult<SessionControlLease> = rpc(
        method = MobileControlMethods.RENEW,
        params = request.controlParams(includeLease = true),
        decode = ::decodeLease,
    )

    suspend fun status(
        request: SessionControlStatusRequest,
    ): SessionControllerResult<SessionControlStatus> = rpc(
        method = MobileControlMethods.STATUS,
        params = request.controlParams(),
        decode = ::decodeStatus,
    )

    suspend fun release(
        request: SessionControlReleaseRequest,
    ): SessionControllerResult<SessionControlReleaseResponse> = rpc(
        method = MobileControlMethods.RELEASE,
        params = request.controlParams(includeLease = true),
        decode = ::decodeRelease,
    )

    suspend fun commandStatus(
        request: SessionCommandStatusRequest,
    ): SessionControllerResult<SessionCommandStatus> = rpc(
        method = MobileControlMethods.COMMAND_STATUS,
        params = buildJsonObject {
            put("session_key", request.sessionKey.value)
            request.profile?.let { put("profile", it) }
            request.runtimeSessionId?.let { put("runtime_session_id", it.value) }
            put("method", request.method)
            put("client_request_id", request.clientRequestId.value)
        },
        decode = { element ->
            decodeCommandStatus(element)?.takeIf { response ->
                response.clientRequestId == request.clientRequestId
            }
        },
    )

    suspend fun submitPrompt(
        request: PromptSubmitRequest,
    ): SessionControllerResult<PromptSubmitResponse> = rpc(
        method = MobileControlMethods.PROMPT_SUBMIT,
        params = buildJsonObject {
            putMutationScope(
                sessionKey = request.sessionKey,
                runtimeSessionId = request.runtimeSessionId,
                leaseId = request.leaseId,
                clientRequestId = request.clientRequestId,
            )
            put("client_turn_id", request.clientTurnId.value)
            put("text", request.text)
        },
        decode = { element ->
            decodePromptSubmit(element)?.takeIf { response ->
                response.clientRequestId == request.clientRequestId &&
                    response.clientTurnId == request.clientTurnId
            }
        },
    )

    suspend fun promptSubmit(
        request: PromptSubmitRequest,
    ): SessionControllerResult<PromptSubmitResponse> = submitPrompt(request)

    suspend fun interrupt(
        request: SessionInterruptRequest,
    ): SessionControllerResult<SessionInterruptResponse> = rpc(
        method = MobileControlMethods.SESSION_INTERRUPT,
        params = buildJsonObject {
            putMutationScope(
                sessionKey = request.sessionKey,
                runtimeSessionId = request.runtimeSessionId,
                leaseId = request.leaseId,
                clientRequestId = request.clientRequestId,
            )
        },
        decode = { element ->
            decodeInterrupt(element)?.takeIf { response ->
                response.clientRequestId == request.clientRequestId
            }
        },
    )

    suspend fun steer(
        request: SessionSteerRequest,
    ): SessionControllerResult<SessionSteerResponse> = rpc(
        method = MobileControlMethods.SESSION_STEER,
        params = buildJsonObject {
            putMutationScope(
                sessionKey = request.sessionKey,
                runtimeSessionId = request.runtimeSessionId,
                leaseId = request.leaseId,
                clientRequestId = request.clientRequestId,
            )
            put("text", request.text)
        },
        decode = { element ->
            decodeSteer(element)?.takeIf { response ->
                response.clientRequestId == request.clientRequestId
            }
        },
    )

    suspend fun respondApproval(
        request: ApprovalRespondRequest,
    ): SessionControllerResult<PendingInputRespondResponse> = rpc(
        method = MobileControlMethods.APPROVAL_RESPOND,
        params = buildJsonObject {
            putMutationScope(
                sessionKey = request.sessionKey,
                runtimeSessionId = request.runtimeSessionId,
                leaseId = request.leaseId,
                clientRequestId = request.clientRequestId,
            )
            put("request_id", request.requestId)
            put("choice", request.choice.wireValue)
        },
        decode = { element ->
            decodePendingInputResponse(
                element = element,
                expectedKind = PendingInputKind.APPROVAL,
                expectedRequestId = request.requestId,
                expectedClientRequestId = request.clientRequestId,
            )
        },
    )

    suspend fun respondClarify(
        request: ClarifyRespondRequest,
    ): SessionControllerResult<PendingInputRespondResponse> = rpc(
        method = MobileControlMethods.CLARIFY_RESPOND,
        params = buildJsonObject {
            putMutationScope(
                sessionKey = request.scope.sessionKey,
                runtimeSessionId = request.scope.runtimeSessionId,
                leaseId = request.scope.leaseId,
                clientRequestId = request.clientRequestId,
            )
            put("request_id", request.scope.requestId)
            when (val answer = request.answer) {
                is SessionClarifyAnswer.Choice -> put("choice_id", answer.choiceId)
                is SessionClarifyAnswer.Other -> put("other_text", answer.text)
            }
        },
        decode = { element ->
            decodePendingInputResponse(
                element = element,
                expectedKind = PendingInputKind.CLARIFY,
                expectedRequestId = request.scope.requestId,
                expectedClientRequestId = request.clientRequestId,
            )
        },
    )

    fun close(code: Int = NORMAL_CLOSE_CODE) {
        gatewayConnection.close(code)
    }

    internal fun onGatewayStateChanged(state: GatewaySocketState) {
        if (state == GatewaySocketState.Ready) {
            val capabilities = gatewayConnection.capabilities
            if (capabilities?.supportsSessionControl(CONTROL_CONTRACT_VERSION) == true) {
                ready = SessionControlReady(
                    controlContractVersion = requireNotNull(capabilities.controlContractVersion),
                    connectionRole = requireNotNull(capabilities.connectionRole),
                    availableMethods = requireNotNull(capabilities.controlAvailableMethods),
                    errorCodes = requireNotNull(capabilities.controlErrorCodes),
                )
                safely { observer.onStateChanged(GatewaySocketState.Ready) }
            } else {
                handshakeRejected = true
                ready = null
                safely(observer::onProtocolError)
                gatewayConnection.close(PROTOCOL_ERROR_CLOSE_CODE)
            }
            return
        }
        if (state is GatewaySocketState.Closed || state is GatewaySocketState.Failed) {
            ready = null
        }
        safely { observer.onStateChanged(state) }
    }

    internal fun onGatewayEvent(event: GatewayEvent) {
        if (event.type != GATEWAY_READY_EVENT || ready != null) {
            safely { observer.onEvent(event) }
        }
    }

    internal fun onGatewayProtocolError() {
        safely(observer::onProtocolError)
    }

    private suspend fun <T> rpc(
        method: String,
        params: JsonObject,
        decode: (JsonElement) -> T?,
    ): SessionControllerResult<T> {
        if (handshakeRejected) return SessionControllerResult.Unsupported
        val readySnapshot = ready ?: return SessionControllerResult.NotReady
        if (!readySnapshot.supports(method)) return SessionControllerResult.Unsupported
        return when (val result = gatewayConnection.call(method, params)) {
            is GatewayCallResult.Success -> {
                val value = runCatching { decode(result.value) }.getOrNull()
                    ?: return SessionControllerResult.InvalidResponse
                SessionControllerResult.Success(value)
            }
            is GatewayCallResult.RpcFailure -> SessionControllerResult.RpcFailure(result.error)
            GatewayCallResult.NotReady -> SessionControllerResult.NotReady
            GatewayCallResult.Disconnected -> SessionControllerResult.Disconnected
            GatewayCallResult.Timeout -> SessionControllerResult.Timeout
        }
    }

    private inline fun safely(action: () -> Unit) {
        try {
            action()
        } catch (_: RuntimeException) {
            // Controller observers cannot break handshake verification or socket dispatch.
        }
    }

    override fun toString(): String =
        "SessionControllerConnection(state=$state, ready=$ready)"

    private companion object {
        const val CONTROL_CONTRACT_VERSION = 1
        const val GATEWAY_READY_EVENT = "gateway.ready"
        const val NORMAL_CLOSE_CODE = 1000
        const val PROTOCOL_ERROR_CLOSE_CODE = 1002
    }
}

private class SessionControllerObserverBridge(
    private val observer: SessionControllerObserver,
) : GatewaySocketObserver {
    private val lock = Any()
    private var connection: SessionControllerConnection? = null
    private val pending = ArrayDeque<(SessionControllerConnection) -> Unit>()

    fun attach(connection: SessionControllerConnection) {
        val queued = synchronized(lock) {
            this.connection = connection
            pending.toList().also { pending.clear() }
        }
        queued.forEach { it(connection) }
    }

    override fun onStateChanged(state: GatewaySocketState) {
        dispatch { it.onGatewayStateChanged(state) }
    }

    override fun onEvent(event: GatewayEvent) {
        dispatch { it.onGatewayEvent(event) }
    }

    override fun onProtocolError() {
        dispatch(SessionControllerConnection::onGatewayProtocolError)
    }

    private fun dispatch(action: (SessionControllerConnection) -> Unit) {
        val target = synchronized(lock) {
            connection.also { if (it == null) pending.addLast(action) }
        }
        target?.let(action)
    }
}

private fun SessionControlRenewRequest.controlParams(includeLease: Boolean): JsonObject =
    buildJsonObject {
        put("session_key", sessionKey.value)
        profile?.let { put("profile", it) }
        runtimeSessionId?.let { put("runtime_session_id", it.value) }
        clientInstanceId?.let { put("client_instance_id", it.value) }
        if (includeLease) put("lease_id", leaseId.value)
    }

private fun SessionControlStatusRequest.controlParams(): JsonObject =
    buildJsonObject {
        put("session_key", sessionKey.value)
        profile?.let { put("profile", it) }
        runtimeSessionId?.let { put("runtime_session_id", it.value) }
        clientInstanceId?.let { put("client_instance_id", it.value) }
    }

private fun SessionControlReleaseRequest.controlParams(includeLease: Boolean): JsonObject =
    buildJsonObject {
        put("session_key", sessionKey.value)
        profile?.let { put("profile", it) }
        runtimeSessionId?.let { put("runtime_session_id", it.value) }
        clientInstanceId?.let { put("client_instance_id", it.value) }
        if (includeLease) put("lease_id", leaseId.value)
    }

private fun kotlinx.serialization.json.JsonObjectBuilder.putMutationScope(
    sessionKey: SessionKey,
    runtimeSessionId: RuntimeSessionId?,
    leaseId: SessionControlLeaseId,
    clientRequestId: ClientRequestId,
) {
    put("session_key", sessionKey.value)
    runtimeSessionId?.let { put("runtime_session_id", it.value) }
    put("lease_id", leaseId.value)
    put("client_request_id", clientRequestId.value)
}

private data class DecodedPendingInput(val value: SessionPendingInput?)

private fun decodePendingInput(element: JsonElement?): DecodedPendingInput? {
    return when (element) {
        JsonNull -> DecodedPendingInput(null)
        is JsonObject -> when (element.requiredString("kind")) {
            "approval" -> DecodedPendingInput(decodePendingApproval(element) ?: return null)
            "clarify" -> DecodedPendingInput(decodePendingClarify(element) ?: return null)
            else -> null
        }
        else -> null
    }
}

private fun decodePendingApproval(value: JsonObject): SessionPendingInput.Approval? {
    val choices = (value["choices"] as? JsonArray)?.map { rawChoice ->
        val choice = (rawChoice as? JsonPrimitive)
            ?.takeIf { it.isString }
            ?.content
            ?: return null
        SessionApprovalChoice.fromWireValue(choice) ?: return null
    } ?: return null
    return SessionPendingInput.Approval(
        requestId = value.requiredString("request_id") ?: return null,
        title = value.requiredString("title") ?: return null,
        description = value.requiredStringAllowBlank("description") ?: return null,
        command = value.requiredStringAllowBlank("command") ?: return null,
        choices = choices,
        expiresAtEpochMs = value.requiredNonNegativeLong("expires_at_epoch_ms") ?: return null,
    )
}

private fun decodePendingClarify(value: JsonObject): SessionPendingInput.Clarify? {
    val choices = (value["choices"] as? JsonArray)?.map { rawChoice ->
        val choice = rawChoice as? JsonObject ?: return null
        SessionClarifyChoice(
            id = choice.requiredString("id") ?: return null,
            label = choice.requiredString("label") ?: return null,
        )
    } ?: return null
    return SessionPendingInput.Clarify(
        requestId = value.requiredString("request_id") ?: return null,
        question = value.requiredString("question") ?: return null,
        choices = choices,
        allowOther = value.requiredBoolean("allow_other") ?: return null,
        expiresAtEpochMs = value.requiredNonNegativeLong("expires_at_epoch_ms") ?: return null,
    )
}

private fun decodeLease(element: JsonElement): SessionControlLease? {
    val value = element as? JsonObject ?: return null
    val pendingInput = (decodePendingInput(value["pending_input"]) ?: return null).value
    return SessionControlLease(
        leaseId = SessionControlLeaseId(value.requiredString("lease_id") ?: return null),
        expiresAtEpochMs = value.requiredNonNegativeLong("expires_at_epoch_ms") ?: return null,
        controlRevision = value.requiredNonNegativeLong("control_revision") ?: return null,
        controllerKind = SessionControllerKind.fromWireValue(value.requiredString("controller_kind"))
            ?.takeIf { it == SessionControllerKind.MOBILE }
            ?: return null,
        controllerLabel = value.requiredString("controller_label") ?: return null,
        pendingInput = pendingInput,
    )
}

private fun decodeStatus(element: JsonElement): SessionControlStatus? {
    val value = element as? JsonObject ?: return null
    val label = value.optionalString("controller_label")
    if (value["controller_label"] !is JsonNull && label == null) return null
    val controllerKind = SessionControllerKind.normalizeStatusWireValue(
        value.requiredString("controller_kind"),
    ) ?: return null
    if (
        (controllerKind == SessionControllerKind.NONE && label != null) ||
        (controllerKind != SessionControllerKind.NONE && label.isNullOrBlank())
    ) {
        return null
    }
    val pendingInput = (decodePendingInput(value["pending_input"]) ?: return null).value
    return SessionControlStatus(
        controllerKind = controllerKind,
        controllerLabel = label,
        controlRevision = value.requiredNonNegativeLong("control_revision") ?: return null,
        leaseExpiresAtEpochMs = value.requiredNonNegativeLong("lease_expires_at_epoch_ms") ?: return null,
        pendingInput = pendingInput,
    )
}

private fun decodeRelease(element: JsonElement): SessionControlReleaseResponse? {
    val value = element as? JsonObject ?: return null
    return SessionControlReleaseResponse(
        released = (value["released"] as? JsonPrimitive)?.booleanOrNull ?: return null,
        controlRevision = value.requiredNonNegativeLong("control_revision") ?: return null,
    )
}

private fun decodeCommandStatus(element: JsonElement): SessionCommandStatus? {
    val value = element as? JsonObject ?: return null
    if (
        !value.keys.containsAll(setOf("status", "client_request_id")) ||
        !setOf("status", "client_request_id", "client_turn_id", "server_turn_id")
            .containsAll(value.keys)
    ) {
        return null
    }
    val status = SessionCommandState.fromWireValue(value.requiredString("status"))
        ?.takeIf { it != SessionCommandState.UNKNOWN }
        ?: return null
    return SessionCommandStatus(
        status = status,
        clientRequestId = ClientRequestId(value.requiredString("client_request_id") ?: return null),
        clientTurnId = value.optionalString("client_turn_id")?.let(::ClientTurnId),
        serverTurnId = value.optionalString("server_turn_id")?.let(::ServerTurnId),
    )
}

private fun decodePromptSubmit(element: JsonElement): PromptSubmitResponse? {
    val value = element as? JsonObject ?: return null
    val status = SessionCommandState.fromWireValue(value.requiredString("status"))
        ?.takeIf { it != SessionCommandState.UNKNOWN }
        ?: return null
    return PromptSubmitResponse(
        status = status,
        clientRequestId = ClientRequestId(value.requiredString("client_request_id") ?: return null),
        clientTurnId = ClientTurnId(value.requiredString("client_turn_id") ?: return null),
        serverTurnId = value.optionalString("server_turn_id")?.let(::ServerTurnId),
    )
}

private fun decodeInterrupt(element: JsonElement): SessionInterruptResponse? {
    val value = element as? JsonObject ?: return null
    val status = SessionCommandState.fromWireValue(value.requiredString("status"))
        ?.takeIf { it != SessionCommandState.UNKNOWN }
        ?: return null
    return SessionInterruptResponse(
        status = status,
        clientRequestId = ClientRequestId(value.requiredString("client_request_id") ?: return null),
    )
}

private fun decodeSteer(element: JsonElement): SessionSteerResponse? {
    val value = element as? JsonObject ?: return null
    val status = SessionCommandState.fromWireValue(value.requiredString("status"))
        ?.takeIf { it != SessionCommandState.UNKNOWN }
        ?: return null
    return SessionSteerResponse(
        status = status,
        clientRequestId = ClientRequestId(value.requiredString("client_request_id") ?: return null),
    )
}

private fun decodePendingInputResponse(
    element: JsonElement,
    expectedKind: PendingInputKind,
    expectedRequestId: String,
    expectedClientRequestId: ClientRequestId,
): PendingInputRespondResponse? {
    val value = element as? JsonObject ?: return null
    if (value.requiredString("status") != "accepted") return null
    if (value.requiredString("kind") != expectedKind.wireValue) return null
    val requestId = value.requiredString("request_id")
        ?.takeIf { it == expectedRequestId }
        ?: return null
    val clientRequestId = value.requiredString("client_request_id")
        ?.takeIf { it == expectedClientRequestId.value }
        ?.let(::ClientRequestId)
        ?: return null
    return PendingInputRespondResponse(
        kind = expectedKind,
        requestId = requestId,
        clientRequestId = clientRequestId,
        controlRevision = value.requiredNonNegativeLong("control_revision") ?: return null,
    )
}

private fun JsonObject.requiredString(key: String): String? =
    requiredStringAllowBlank(key)?.takeIf(String::isNotBlank)

private fun JsonObject.requiredStringAllowBlank(key: String): String? =
    (get(key) as? JsonPrimitive)
        ?.takeIf { it.isString }
        ?.content

private fun JsonObject.optionalString(key: String): String? = when (val raw = get(key)) {
    null, JsonNull -> null
    is JsonPrimitive -> raw.takeIf { it.isString }?.content?.takeIf(String::isNotBlank)
    else -> null
}

private fun JsonObject.requiredBoolean(key: String): Boolean? =
    (get(key) as? JsonPrimitive)
        ?.takeUnless { it.isString }
        ?.booleanOrNull

private fun JsonObject.requiredNonNegativeLong(key: String): Long? =
    (get(key) as? JsonPrimitive)
        ?.takeUnless { it.isString }
        ?.longOrNull
        ?.takeIf { it >= 0 }
