package app.hermesmobile.sessions

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.auth.ScopedWebSocketTicket
import app.hermesmobile.protocol.auth.ScopedWebSocketTicketRequest
import app.hermesmobile.protocol.gateway.GatewayConnectionRole
import app.hermesmobile.protocol.gateway.GatewayWebSocketClient
import app.hermesmobile.protocol.gateway.ClientRequestId
import app.hermesmobile.protocol.gateway.MobileControlMethods
import app.hermesmobile.protocol.gateway.PendingInputKind
import app.hermesmobile.protocol.gateway.PendingInputRespondResponse
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyAnswer
import app.hermesmobile.protocol.gateway.SessionCommandState
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControlStatus
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionControllerClient
import app.hermesmobile.protocol.gateway.SessionControllerResult
import app.hermesmobile.protocol.gateway.SessionSteerResponse
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionProjection
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Before
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class GatewaySessionControlSourceTest {
    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `control status repeats exact immutable ticket and runtime binding`() = runBlocking {
        val rpcRequest = CompletableDeferred<JsonObject>()
        server.enqueue(
            controlSocket { webSocket, request ->
                rpcRequest.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"controller_kind":"desktop","controller_label":"Hermes Desktop","control_revision":8,"lease_expires_at_epoch_ms":0,"pending_input":null}}""",
                )
            },
        )
        val clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111")
        val source = GatewaySessionControlSource(
            endpoint = endpoint(),
            ticketSource = ScopedWebSocketTicketSource {
                ScopedWebSocketTicketResult.Ready(
                    ScopedWebSocketTicket(
                        value = "control-ticket-secret",
                        ttlSeconds = 30,
                        connectionRole = GatewayConnectionRole.CONTROL,
                    ),
                )
            },
            controllerClient = SessionControllerClient(
                GatewayWebSocketClient(
                    OkHttpClient.Builder()
                        .readTimeout(0, TimeUnit.MILLISECONDS)
                        .build(),
                ),
            ),
            clientInstanceId = clientInstanceId,
        )
        val channel = assertIs<SessionControlOpenResult.Ready>(
            source.open(session(profile = "fox"), RuntimeSessionId("runtime-1")),
        ).channel

        val status = assertIs<SessionControllerResult.Success<SessionControlStatus>>(
            channel.status(),
        ).value

        val request = withTimeout(3_000) { rpcRequest.await() }
        assertEquals(MobileControlMethods.STATUS, request.getValue("method").jsonPrimitive.content)
        val params = request.getValue("params").jsonObject
        assertEquals(
            setOf("session_key", "profile", "runtime_session_id", "client_instance_id"),
            params.keys,
        )
        assertEquals("stored-1", params.getValue("session_key").jsonPrimitive.content)
        assertEquals("fox", params.getValue("profile").jsonPrimitive.content)
        assertEquals("runtime-1", params.getValue("runtime_session_id").jsonPrimitive.content)
        assertEquals(clientInstanceId.value, params.getValue("client_instance_id").jsonPrimitive.content)
        assertEquals(SessionControllerKind.DESKTOP, status.controllerKind)
        assertEquals("Hermes Desktop", status.controllerLabel)
        channel.close()
    }

    @Test
    fun `opening control mints exact scoped ticket and binds acquire to observed runtime`() = runBlocking {
        val ticketRequests = mutableListOf<ScopedWebSocketTicketRequest>()
        val rpcRequest = CompletableDeferred<JsonObject>()
        server.enqueue(
            controlSocket { webSocket, request ->
                rpcRequest.complete(request)
                val id = request.getValue("id").jsonPrimitive.content
                webSocket.send(
                    """{"jsonrpc":"2.0","id":$id,"result":{"lease_id":"lease-secret","expires_at_epoch_ms":1900000000000,"control_revision":7,"controller_kind":"mobile","controller_label":"Hermes Mobile","pending_input":null}}""",
                )
            },
        )
        val clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111")
        val source = GatewaySessionControlSource(
            endpoint = endpoint(),
            ticketSource = ScopedWebSocketTicketSource { request ->
                ticketRequests += request
                ScopedWebSocketTicketResult.Ready(
                    ScopedWebSocketTicket(
                        value = "control-ticket-secret",
                        ttlSeconds = 30,
                        connectionRole = GatewayConnectionRole.CONTROL,
                    ),
                )
            },
            controllerClient = SessionControllerClient(
                GatewayWebSocketClient(
                    OkHttpClient.Builder()
                        .readTimeout(0, TimeUnit.MILLISECONDS)
                        .build(),
                ),
            ),
            clientInstanceId = clientInstanceId,
        )
        val session = session(profile = "fox")
        val runtime = RuntimeSessionId("runtime-1")

        val opened = source.open(session, runtime)
        val channel = assertIs<SessionControlOpenResult.Ready>(opened).channel
        assertEquals(MobileControlMethods.IMPLEMENTED, channel.availableMethods)
        val lease = assertIs<SessionControllerResult.Success<SessionControlLease>>(
            channel.acquire(),
        ).value

        assertEquals(
            listOf(
                ScopedWebSocketTicketRequest(
                    connectionRole = GatewayConnectionRole.CONTROL,
                    clientInstanceId = clientInstanceId,
                    sessionKey = session.sessionKey,
                    profile = "fox",
                ),
            ),
            ticketRequests,
        )
        val request = withTimeout(3_000) { rpcRequest.await() }
        assertEquals(MobileControlMethods.ACQUIRE, request.getValue("method").jsonPrimitive.content)
        assertEquals("stored-1", request.getValue("params").jsonObject.getValue("session_key").jsonPrimitive.content)
        assertEquals("fox", request.getValue("params").jsonObject.getValue("profile").jsonPrimitive.content)
        assertEquals("runtime-1", request.getValue("params").jsonObject.getValue("runtime_session_id").jsonPrimitive.content)
        assertEquals(clientInstanceId.value, request.getValue("params").jsonObject.getValue("client_instance_id").jsonPrimitive.content)
        assertEquals(7L, lease.controlRevision)
        channel.close()
    }

    @Test
    fun `pending responses forward exact control identity and typed answer`() = runBlocking {
        val requests = java.util.Collections.synchronizedList(mutableListOf<JsonObject>())
        server.enqueue(
            controlSocket { webSocket, request ->
                requests += request
                val id = request.getValue("id").toString()
                val result = when (request.getValue("method").jsonPrimitive.content) {
                    MobileControlMethods.ACQUIRE ->
                        """{"lease_id":"lease-secret","expires_at_epoch_ms":1900000000000,"control_revision":7,"controller_kind":"mobile","controller_label":"Hermes Mobile","pending_input":null}"""
                    MobileControlMethods.APPROVAL_RESPOND ->
                        """{"status":"accepted","kind":"approval","request_id":"approval-1","client_request_id":"client-approval-1","control_revision":8}"""
                    MobileControlMethods.CLARIFY_RESPOND ->
                        """{"status":"accepted","kind":"clarify","request_id":"clarify-1","client_request_id":"client-clarify-1","control_revision":9}"""
                    else -> error("Unexpected control method")
                }
                webSocket.send("""{"jsonrpc":"2.0","id":$id,"result":$result}""")
            },
        )
        val source = GatewaySessionControlSource(
            endpoint = endpoint(),
            ticketSource = ScopedWebSocketTicketSource {
                ScopedWebSocketTicketResult.Ready(
                    ScopedWebSocketTicket(
                        value = "control-ticket-secret",
                        ttlSeconds = 30,
                        connectionRole = GatewayConnectionRole.CONTROL,
                    ),
                )
            },
            controllerClient = SessionControllerClient(
                GatewayWebSocketClient(
                    OkHttpClient.Builder()
                        .readTimeout(0, TimeUnit.MILLISECONDS)
                        .build(),
                ),
            ),
            clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
        )
        val channel = assertIs<SessionControlOpenResult.Ready>(
            source.open(session(profile = "fox"), RuntimeSessionId("runtime-1")),
        ).channel
        assertIs<SessionControllerResult.Success<SessionControlLease>>(channel.acquire())

        assertEquals(
            PendingInputRespondResponse(
                kind = PendingInputKind.APPROVAL,
                requestId = "approval-1",
                clientRequestId = ClientRequestId("client-approval-1"),
                controlRevision = 8,
            ),
            assertIs<SessionControllerResult.Success<PendingInputRespondResponse>>(
                channel.respondApproval(
                    leaseId = SessionControlLeaseId("lease-secret"),
                    clientRequestId = ClientRequestId("client-approval-1"),
                    requestId = "approval-1",
                    choice = SessionApprovalChoice.ALLOW_ONCE,
                ),
            ).value,
        )
        assertEquals(
            PendingInputRespondResponse(
                kind = PendingInputKind.CLARIFY,
                requestId = "clarify-1",
                clientRequestId = ClientRequestId("client-clarify-1"),
                controlRevision = 9,
            ),
            assertIs<SessionControllerResult.Success<PendingInputRespondResponse>>(
                channel.respondClarify(
                    leaseId = SessionControlLeaseId("lease-secret"),
                    clientRequestId = ClientRequestId("client-clarify-1"),
                    requestId = "clarify-1",
                    answer = SessionClarifyAnswer.Choice("choice-1"),
                ),
            ).value,
        )

        val approvalParams = requests.single {
            it.getValue("method").jsonPrimitive.content == MobileControlMethods.APPROVAL_RESPOND
        }.getValue("params").jsonObject
        assertEquals(
            setOf(
                "session_key",
                "runtime_session_id",
                "lease_id",
                "client_request_id",
                "request_id",
                "choice",
            ),
            approvalParams.keys,
        )
        assertEquals("stored-1", approvalParams.getValue("session_key").jsonPrimitive.content)
        assertEquals("runtime-1", approvalParams.getValue("runtime_session_id").jsonPrimitive.content)
        assertEquals("lease-secret", approvalParams.getValue("lease_id").jsonPrimitive.content)
        assertEquals("client-approval-1", approvalParams.getValue("client_request_id").jsonPrimitive.content)
        assertEquals("approval-1", approvalParams.getValue("request_id").jsonPrimitive.content)
        assertEquals("allow_once", approvalParams.getValue("choice").jsonPrimitive.content)

        val clarifyParams = requests.single {
            it.getValue("method").jsonPrimitive.content == MobileControlMethods.CLARIFY_RESPOND
        }.getValue("params").jsonObject
        assertEquals(
            setOf(
                "session_key",
                "runtime_session_id",
                "lease_id",
                "client_request_id",
                "request_id",
                "choice_id",
            ),
            clarifyParams.keys,
        )
        assertEquals("choice-1", clarifyParams.getValue("choice_id").jsonPrimitive.content)
        channel.close()
    }

    @Test
    fun `guidance forwards through steer with the bound session runtime and lease`() = runBlocking {
        val requests = java.util.Collections.synchronizedList(mutableListOf<JsonObject>())
        server.enqueue(
            controlSocket { webSocket, request ->
                requests += request
                val id = request.getValue("id").toString()
                val result = when (request.getValue("method").jsonPrimitive.content) {
                    MobileControlMethods.ACQUIRE ->
                        """{"lease_id":"lease-secret","expires_at_epoch_ms":1900000000000,"control_revision":7,"controller_kind":"mobile","controller_label":"Hermes Mobile","pending_input":null}"""
                    MobileControlMethods.SESSION_STEER ->
                        """{"status":"accepted","client_request_id":"steer-1"}"""
                    else -> error("Unexpected control method")
                }
                webSocket.send("""{"jsonrpc":"2.0","id":$id,"result":$result}""")
            },
        )
        val source = GatewaySessionControlSource(
            endpoint = endpoint(),
            ticketSource = ScopedWebSocketTicketSource {
                ScopedWebSocketTicketResult.Ready(
                    ScopedWebSocketTicket(
                        value = "control-ticket-secret",
                        ttlSeconds = 30,
                        connectionRole = GatewayConnectionRole.CONTROL,
                    ),
                )
            },
            controllerClient = SessionControllerClient(
                GatewayWebSocketClient(
                    OkHttpClient.Builder()
                        .readTimeout(0, TimeUnit.MILLISECONDS)
                        .build(),
                ),
            ),
            clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
        )
        val channel = assertIs<SessionControlOpenResult.Ready>(
            source.open(session(profile = "fox"), RuntimeSessionId("runtime-1")),
        ).channel
        assertIs<SessionControllerResult.Success<SessionControlLease>>(channel.acquire())

        assertEquals(
            SessionSteerResponse(
                status = SessionCommandState.ACCEPTED,
                clientRequestId = ClientRequestId("steer-1"),
            ),
            assertIs<SessionControllerResult.Success<SessionSteerResponse>>(
                channel.steer(
                    leaseId = SessionControlLeaseId("lease-secret"),
                    requestId = ClientRequestId("steer-1"),
                    text = "Keep the current turn, but verify authorization too",
                ),
            ).value,
        )

        val params = requests.single {
            it.getValue("method").jsonPrimitive.content == MobileControlMethods.SESSION_STEER
        }.getValue("params").jsonObject
        assertEquals(
            setOf("session_key", "runtime_session_id", "lease_id", "client_request_id", "text"),
            params.keys,
        )
        assertEquals("stored-1", params.getValue("session_key").jsonPrimitive.content)
        assertEquals("runtime-1", params.getValue("runtime_session_id").jsonPrimitive.content)
        assertEquals("lease-secret", params.getValue("lease_id").jsonPrimitive.content)
        assertEquals("steer-1", params.getValue("client_request_id").jsonPrimitive.content)
        assertEquals(
            "Keep the current turn, but verify authorization too",
            params.getValue("text").jsonPrimitive.content,
        )
        channel.close()
    }

    @Test
    fun `cancelling control open before ready closes the websocket`() = runBlocking {
        val serverObservedClose = CompletableDeferred<Unit>()
        server.enqueue(
            MockResponse()
                .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
                .withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        serverObservedClose.complete(Unit)
                        webSocket.close(code, reason)
                    }
                },
            ),
        )
        val source = GatewaySessionControlSource(
            endpoint = endpoint(),
            ticketSource = ScopedWebSocketTicketSource {
                ScopedWebSocketTicketResult.Ready(
                    ScopedWebSocketTicket(
                        value = "control-ticket-secret",
                        ttlSeconds = 30,
                        connectionRole = GatewayConnectionRole.CONTROL,
                    ),
                )
            },
            controllerClient = SessionControllerClient(
                GatewayWebSocketClient(
                    OkHttpClient.Builder()
                        .readTimeout(0, TimeUnit.MILLISECONDS)
                        .build(),
                ),
            ),
            clientInstanceId = ClientInstanceId("11111111-1111-4111-8111-111111111111"),
        )

        val opening = launch(Dispatchers.Default) {
            source.open(session(profile = "fox"), RuntimeSessionId("runtime-1"))
        }
        check(server.takeRequest(3, TimeUnit.SECONDS) != null)
        opening.cancelAndJoin()

        withTimeout(3_000) { serverObservedClose.await() }
    }

    private fun endpoint(): GatewayEndpoint =
        GatewayEndpoint.parse(server.url("/base/").toString()).getOrThrow()

    private fun controlSocket(
        onRequest: (WebSocket, JsonObject) -> Unit,
    ): MockResponse = MockResponse()
        .setHeader("Sec-WebSocket-Protocol", "hermes.tui.v1")
        .withWebSocketUpgrade(
        object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(
                    """{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"observer_contract":1,"control_contract":1,"connection_role":"control","control_available_methods":["approval.respond","clarify.respond","prompt.submit","session.command.status","session.control.acquire","session.control.release","session.control.renew","session.control.status","session.interrupt","session.steer"],"control_error_codes":{"control_role_required":4200,"control_contract_unsupported":4201,"live_runtime_unavailable":4202,"controller_conflict":4203,"lease_required":4204,"lease_expired":4205,"lease_mismatch":4206,"request_id_payload_conflict":4207,"pending_request_conflict":4208,"method_not_allowed":4209,"command_unknown":4210,"revision_conflict":4211,"session_binding_mismatch":4212,"invalid_pending_response":4213,"owner_adapter_unavailable":4214,"relay_overloaded":4215,"deadline_exceeded_before_effect":4306,"effect_unknown":4307}}}}""",
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                onRequest(webSocket, Json.parseToJsonElement(text).jsonObject)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        },
    )

    private fun session(profile: String?) = SessionProjection(
        sessionKey = SessionKey("stored-1"),
        lineageRoot = SessionKey("stored-1"),
        lineageTip = SessionKey("stored-1"),
        parentSessionKey = null,
        title = "First session",
        preview = "Preview",
        source = "desktop",
        model = "test-model",
        profile = profile,
        cwd = null,
        gitBranch = null,
        startedAtEpochSeconds = 100.0,
        endedAtEpochSeconds = null,
        lastActiveEpochSeconds = 120.0,
        messageCount = 1,
        toolCallCount = 0,
        inputTokens = 0,
        outputTokens = 0,
        isActive = true,
        archived = false,
    )
}
