package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.WebSocketTicket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.intOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

sealed interface GatewaySocketState {
    data object Connecting : GatewaySocketState
    data object Open : GatewaySocketState
    data object Ready : GatewaySocketState
    data class Closed(val code: Int) : GatewaySocketState
    data class Failed(val summary: String = "Hermes realtime connection failed.") : GatewaySocketState
}

interface GatewaySocketObserver {
    fun onStateChanged(state: GatewaySocketState)
    fun onEvent(event: GatewayEvent)
    fun onProtocolError() = Unit
}

sealed interface GatewayCallResult {
    data class Success(val value: JsonElement) : GatewayCallResult
    data class RpcFailure(val error: JsonRpcError) : GatewayCallResult
    data object NotReady : GatewayCallResult
    data object Disconnected : GatewayCallResult
    data object Timeout : GatewayCallResult
}

data class GatewayCapabilities(
    val observerContractVersion: Int?,
    val controlContractVersion: Int? = null,
    val connectionRole: GatewayConnectionRole? = null,
    val controlAvailableMethods: Set<String>? = null,
    val controlErrorCodes: Map<String, Int>? = null,
) {
    fun supportsSessionObserver(contractVersion: Int = 2): Boolean =
        observerContractVersion == contractVersion &&
            connectionRole == GatewayConnectionRole.OBSERVER

    fun supportsSessionControl(contractVersion: Int = 1): Boolean =
        observerContractVersion == 1 &&
            controlContractVersion == contractVersion &&
            connectionRole == GatewayConnectionRole.CONTROL &&
            controlAvailableMethods == MobileControlMethods.IMPLEMENTED &&
            controlErrorCodes?.let(MobileControlErrorCodes::isValidAdvertisement) == true
}

class GatewayWebSocketClient(
    private val httpClient: OkHttpClient,
    private val codec: JsonRpcCodec = JsonRpcCodec(),
    private val callTimeoutMillis: Long = DEFAULT_CALL_TIMEOUT_MILLIS,
) {
    fun connect(
        endpoint: GatewayEndpoint,
        ticket: WebSocketTicket,
        observer: GatewaySocketObserver,
        observerContractVersion: Int = 1,
    ): GatewayConnection {
        require(ticket.value.isNotBlank()) { "WebSocket ticket is required." }
        require(callTimeoutMillis > 0) { "JSON-RPC call timeout must be positive." }
        require(observerContractVersion == 1 || observerContractVersion == 2) {
            "WebSocket observer contract version must be supported."
        }
        val subprotocol = when (observerContractVersion) {
            1 -> HERMES_SUBPROTOCOL_V1
            else -> HERMES_SUBPROTOCOL_V2
        }
        val connection = GatewayConnection(
            codec = codec,
            observer = observer,
            callTimeoutMillis = callTimeoutMillis,
            expectedObserverContractVersion = observerContractVersion,
        )
        val url = requireNotNull(endpoint.baseUrl.resolve("api/ws"))
            .newBuilder()
            .addQueryParameter("ticket", ticket.value)
            .build()
        val request = Request.Builder()
            .url(url)
            .header("Sec-WebSocket-Protocol", subprotocol)
            .build()
        connection.changeState(GatewaySocketState.Connecting)
        connection.attach(
            httpClient.newWebSocket(
                request,
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        connection.attach(webSocket)
                        if (response.header("Sec-WebSocket-Protocol") != subprotocol) {
                            connection.protocolError()
                            webSocket.close(PROTOCOL_ERROR_CLOSE_CODE, "subprotocol mismatch")
                            return
                        }
                        connection.changeState(GatewaySocketState.Open)
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val frameBytes = runCatching {
                            text.encodeToByteArray(throwOnInvalidSequence = true).size
                        }.getOrNull()
                        if (frameBytes == null) {
                            connection.protocolError()
                            webSocket.close(PROTOCOL_ERROR_CLOSE_CODE, "invalid UTF-8 text")
                            return
                        }
                        if (frameBytes > JsonRpcCodec.MAX_FRAME_BYTES) {
                            connection.protocolError()
                            webSocket.close(MESSAGE_TOO_BIG_CLOSE_CODE, "message too big")
                            return
                        }
                        if (!connection.receive(text)) {
                            webSocket.close(PROTOCOL_ERROR_CLOSE_CODE, "protocol error")
                        }
                    }

                    override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                        connection.protocolError()
                        webSocket.close(PROTOCOL_ERROR_CLOSE_CODE, "binary frames are not supported")
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        connection.disconnected(GatewaySocketState.Closed(code))
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        connection.disconnected(GatewaySocketState.Failed())
                    }
                },
            ),
        )
        return connection
    }

    private companion object {
        const val HERMES_SUBPROTOCOL_V1 = "hermes.tui.v1"
        const val HERMES_SUBPROTOCOL_V2 = "hermes.tui.v2"
        const val DEFAULT_CALL_TIMEOUT_MILLIS = 30_000L
        const val MESSAGE_TOO_BIG_CLOSE_CODE = 1009
        const val PROTOCOL_ERROR_CLOSE_CODE = 1002
    }
}

class GatewayConnection internal constructor(
    private val codec: JsonRpcCodec,
    private val observer: GatewaySocketObserver,
    private val callTimeoutMillis: Long,
    private val expectedObserverContractVersion: Int,
) {
    private val socket = AtomicReference<WebSocket?>()
    private val nextId = AtomicLong(0)
    private val pending = ConcurrentHashMap<Long, CompletableDeferred<JsonRpcInbound>>()

    @Volatile
    var state: GatewaySocketState = GatewaySocketState.Connecting
        private set

    @Volatile
    var capabilities: GatewayCapabilities? = null
        private set

    suspend fun call(
        method: String,
        params: JsonObject = JsonObject(emptyMap()),
    ): GatewayCallResult {
        if (state != GatewaySocketState.Ready) return GatewayCallResult.NotReady
        val id = nextId.incrementAndGet()
        val response = CompletableDeferred<JsonRpcInbound>()
        pending[id] = response
        val sent = socket.get()?.send(codec.encodeRequest(id, method, params)) == true
        if (!sent) {
            pending.remove(id)
            return GatewayCallResult.Disconnected
        }
        try {
            val inbound = withTimeoutOrNull(callTimeoutMillis) { response.await() }
                ?: return GatewayCallResult.Timeout
            return when (inbound) {
                is JsonRpcInbound.Result -> GatewayCallResult.Success(inbound.result)
                is JsonRpcInbound.Error -> GatewayCallResult.RpcFailure(inbound.error)
                else -> GatewayCallResult.Disconnected
            }
        } finally {
            pending.remove(id)
        }
    }

    fun close(code: Int = NORMAL_CLOSE_CODE) {
        socket.getAndSet(null)?.close(code, "client closing")
        disconnected(GatewaySocketState.Closed(code))
    }

    internal fun attach(webSocket: WebSocket) {
        socket.set(webSocket)
    }

    internal fun receive(document: String): Boolean {
        val inbound = codec.decode(document)
        val eventType = (inbound as? JsonRpcInbound.Event)?.event?.type
        if (
            (state != GatewaySocketState.Ready && eventType != GATEWAY_READY_EVENT) ||
            (state == GatewaySocketState.Ready && eventType == GATEWAY_READY_EVENT)
        ) {
            protocolError()
            return false
        }
        return when (inbound) {
            is JsonRpcInbound.Result -> {
                pending.remove(inbound.id)?.complete(inbound)
                true
            }
            is JsonRpcInbound.Error -> {
                val id = inbound.id
                if (id == null) {
                    protocolError()
                    false
                } else {
                    pending.remove(id)?.complete(inbound)
                    true
                }
            }
            is JsonRpcInbound.Event -> {
                val event = inbound.event
                if (event.type == GATEWAY_READY_EVENT) {
                    val parsedCapabilities = parseReadyCapabilities(event.payload)
                    if (
                        parsedCapabilities == null ||
                        parsedCapabilities.observerContractVersion != expectedObserverContractVersion
                    ) {
                        protocolError()
                        return false
                    }
                    capabilities = parsedCapabilities
                    changeState(GatewaySocketState.Ready)
                }
                safely { observer.onEvent(event) }
                true
            }
            is JsonRpcInbound.Notification -> true
            JsonRpcInbound.Invalid -> {
                protocolError()
                false
            }
        }
    }

    internal fun protocolError() {
        safely(observer::onProtocolError)
    }

    internal fun changeState(next: GatewaySocketState) {
        state = next
        safely { observer.onStateChanged(next) }
    }

    internal fun disconnected(next: GatewaySocketState) {
        socket.set(null)
        capabilities = null
        val waiting = pending.values.toList()
        pending.clear()
        waiting.forEach { it.complete(JsonRpcInbound.Invalid) }
        changeState(next)
    }

    private inline fun safely(action: () -> Unit) {
        try {
            action()
        } catch (_: RuntimeException) {
            // Client observers cannot break socket dispatch or request correlation.
        }
    }

    override fun toString(): String = "GatewayConnection(state=$state, pendingCalls=${pending.size})"

    private fun parseReadyCapabilities(payload: JsonObject?): GatewayCapabilities? {
        val nonNullPayload = payload ?: return null
        val role = (nonNullPayload[CONNECTION_ROLE_KEY] as? JsonPrimitive)
            ?.takeIf { it.isString }
            ?.content
            ?.let(GatewayConnectionRole::fromWireValue)
            ?: return null
        val observerContract = (nonNullPayload[OBSERVER_CONTRACT_KEY] as? JsonPrimitive)
            ?.intOrNull
            ?: return null
        return when (role) {
            GatewayConnectionRole.OBSERVER -> {
                if (observerContract !in OBSERVER_CONTRACT_VERSIONS) return null
                if (nonNullPayload.keys != OBSERVER_READY_FIELDS) return null
                GatewayCapabilities(
                    observerContractVersion = observerContract,
                    connectionRole = role,
                )
            }
            GatewayConnectionRole.CONTROL -> {
                if (observerContract != CONTROL_OBSERVER_CONTRACT_VERSION) return null
                if (nonNullPayload.keys != CONTROL_READY_FIELDS) return null
                val controlContract = (nonNullPayload[CONTROL_CONTRACT_KEY] as? JsonPrimitive)
                    ?.intOrNull
                    ?: return null
                if (controlContract != CONTROL_CONTRACT_VERSION) return null
                val methods = (nonNullPayload[CONTROL_AVAILABLE_METHODS_KEY] as? JsonArray)
                    ?.map { element ->
                        (element as? JsonPrimitive)
                            ?.takeIf { it.isString }
                            ?.content
                            ?: return null
                    }
                    ?: return null
                if (
                    methods.distinct().size != methods.size ||
                    methods.toSet() != MobileControlMethods.IMPLEMENTED
                ) {
                    return null
                }
                val errorCodes = (nonNullPayload[CONTROL_ERROR_CODES_KEY] as? JsonObject)
                    ?.mapValues { (_, value) ->
                        (value as? JsonPrimitive)?.intOrNull ?: return null
                    }
                    ?: return null
                if (!MobileControlErrorCodes.isValidAdvertisement(errorCodes)) return null
                GatewayCapabilities(
                    observerContractVersion = observerContract,
                    controlContractVersion = controlContract,
                    connectionRole = role,
                    controlAvailableMethods = methods.toSet(),
                    controlErrorCodes = errorCodes,
                )
            }
        }
    }

    private companion object {
        const val GATEWAY_READY_EVENT = "gateway.ready"
        const val OBSERVER_CONTRACT_KEY = "observer_contract"
        const val CONTROL_CONTRACT_KEY = "control_contract"
        const val CONNECTION_ROLE_KEY = "connection_role"
        const val CONTROL_AVAILABLE_METHODS_KEY = "control_available_methods"
        const val CONTROL_ERROR_CODES_KEY = "control_error_codes"
        const val CONTROL_OBSERVER_CONTRACT_VERSION = 1
        const val CONTROL_CONTRACT_VERSION = 1
        val OBSERVER_CONTRACT_VERSIONS = setOf(1, 2)
        val OBSERVER_READY_FIELDS = setOf(OBSERVER_CONTRACT_KEY, CONNECTION_ROLE_KEY)
        val CONTROL_READY_FIELDS = setOf(
            OBSERVER_CONTRACT_KEY,
            CONTROL_CONTRACT_KEY,
            CONNECTION_ROLE_KEY,
            CONTROL_AVAILABLE_METHODS_KEY,
            CONTROL_ERROR_CODES_KEY,
        )
        const val NORMAL_CLOSE_CODE = 1000
    }
}
